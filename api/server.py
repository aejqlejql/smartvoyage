"""SmartVoyage application API. Run from the parent folder with python -m."""

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from ..infrastructure.config import _load_local_env
from ..product.auth import AuthService, Credentials, Registration
from ..product.booking import BookingService, order_data, plan_data
from ..product.chat import execute_chat, mode
from ..product.export import plan_markdown
from ..product.planning import DESTINATIONS, ROUTES, TripRequest
from ..product.store import Problem, Store, dumps, uid

STATIC = Path(__file__).resolve().parents[1] / "static"
COOKIE = "smartvoyage_session"
_load_local_env()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationRequest(StrictModel):
    title: str = Field(default="新的旅行", min_length=1, max_length=80)


class ChatRequest(StrictModel):
    conversation_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)


class ConversationScope(StrictModel):
    conversation_id: str = Field(min_length=1, max_length=100)


class PlanUpdate(TripRequest):
    version: int = Field(ge=1)


class OrderRequest(StrictModel):
    plan_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    confirmed: bool = False


class Confirmation(StrictModel):
    confirmed: bool = False


class ProfileRequest(StrictModel):
    conversation_id: str
    profile: dict[str, str] = Field(max_length=20)


def ok(data=None):
    return {"status": "success", "data": data}


def create_app(db_path=None):
    @asynccontextmanager
    async def lifespan(app):
        # Release abandoned holds even when no user opens the order center.
        async def expire_loop():
            while True:
                try:
                    await asyncio.to_thread(app.state.booking.expire_orders)
                except Exception:
                    logging.getLogger(__name__).exception("Inventory expiration sweep failed")
                await asyncio.sleep(30)

        worker = asyncio.create_task(expire_loop())
        yield
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    app = FastAPI(title="SmartVoyage · 旅行规划与模拟预订", version="2.0.0", lifespan=lifespan)
    app.state.store = Store(db_path)
    app.state.auth = AuthService(app.state.store)
    app.state.booking = BookingService(app.state.store)
    app.state.mode = mode()

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        request_id = uid("req")
        start = time.monotonic()
        length = request.headers.get("content-length", "0")
        # API accepts small JSON only. Enforce the actual streamed size too.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            try:
                declared = int(length)
            except ValueError:
                return JSONResponse({"status": "error", "code": "invalid_length", "message": "请求格式错误"}, 400)
            if declared > 65536:
                return JSONResponse({"status": "error", "code": "body_too_large", "message": "请求内容过长"}, 413)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    return JSONResponse({"status": "error", "code": "body_too_large", "message": "请求内容过长"}, 413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers.update(
            {
                "X-Request-ID": request_id,
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            }
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        logging.getLogger("smartvoyage.access").info(
            "%s %s %s %s %dms",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            round((time.monotonic() - start) * 1000),
        )
        return response

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        headers = {"Retry-After": "60"} if exc.status == 429 else None
        return JSONResponse({"status": "error", "code": exc.code, "message": exc.message}, exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Never echo passwords, body or full pydantic input fields in errors.
        errors = [{"field": ".".join(map(str, error["loc"])), "message": error["msg"]} for error in exc.errors()]
        return JSONResponse(
            {"status": "error", "code": "validation_error", "message": "请检查填写内容", "errors": errors}, 422
        )

    @app.exception_handler(sqlite3.Error)
    async def database_handler(request, exc):
        logging.getLogger(__name__).exception("Application storage failed")
        return JSONResponse(
            {"status": "error", "code": "storage_unavailable", "message": "应用存储暂时不可用，请稍后重试"}, 503
        )

    def current_user(request: Request):
        user = app.state.auth.authenticate(request.cookies.get(COOKIE))
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(supplied, user["csrf"]):
                raise Problem("csrf_failed", "操作凭证已失效，请刷新页面重试", 403)
        return user

    def auth_limit(request):
        ip = request.client.host if request.client else "unknown"
        app.state.store.limit("auth:" + ip, 12)
        # JSON-only auth endpoints and strict same-site cookies block login CSRF.
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise Problem("json_required", "请使用 JSON 请求", 415)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise Problem("cross_site_request", "不接受跨站登录请求", 403)

    def set_session(response, token):
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            max_age=86400,
            secure=os.getenv("SMARTVOYAGE_COOKIE_SECURE", "0") == "1",
            path="/",
        )

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/health")
    def health():
        with app.state.store.connection() as db:
            db.execute("SELECT 1")
        return {"status": "ok", "version": "2.0.0", "mode": app.state.mode, "simulation": True}

    @app.get("/api/config")
    def config():
        return ok(
            {
                "mode": app.state.mode,
                "simulation": True,
                "quote_ttl_seconds": 900,
                "order_ttl_seconds": 900,
                "origins": sorted({route[0] for route in ROUTES}),
                "destinations": list(DESTINATIONS),
                "supported_days": [2, 7],
                "notice": "价格、库存、订单、支付和退款均为模拟；不会产生真实交易。",
            }
        )

    @app.post("/api/auth/register", status_code=201)
    def register(payload: Registration, request: Request, response: Response):
        auth_limit(request)
        token, data = app.state.auth.register(payload)
        set_session(response, token)
        return ok(data)

    @app.post("/api/auth/login")
    def login(payload: Credentials, request: Request, response: Response):
        auth_limit(request)
        token, data = app.state.auth.login(payload)
        set_session(response, token)
        return ok(data)

    @app.get("/api/auth/me")
    def me(user=Depends(current_user)):
        return ok({"user": {key: user[key] for key in ("id", "email", "name")}, "csrf_token": user["csrf"]})

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, user=Depends(current_user)):
        app.state.auth.logout(request.cookies[COOKIE], user["id"])
        response.delete_cookie(COOKIE, path="/")
        return ok()

    @app.get("/api/conversations")
    def conversations(user=Depends(current_user)):
        with app.state.store.connection() as db:
            return ok(
                [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,title,created_at FROM conversations "
                        "WHERE user_id=? ORDER BY created_at DESC,rowid DESC",
                        (user["id"],),
                    )
                ]
            )

    @app.post("/api/conversations", status_code=201)
    def new_conversation(payload: ConversationRequest, user=Depends(current_user)):
        return ok(app.state.store.create_conversation(user["id"], payload.title))

    @app.get("/api/conversations/{cid}/messages")
    def messages(cid: str, user=Depends(current_user)):
        with app.state.store.connection() as db:
            app.state.store.owned(db, "conversations", cid, user["id"])
            return ok(
                [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,role,content,created_at FROM "
                        "(SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 200) ORDER BY id",
                        (cid,),
                    )
                ]
            )

    @app.post("/api/chat")
    async def chat(payload: ChatRequest, user=Depends(current_user)):
        app.state.store.limit("chat:" + user["id"], 20)
        if not payload.message.strip():
            raise Problem("empty_message", "消息不能为空", 422)
        return ok(await execute_chat(app.state.store, user["id"], payload.conversation_id, payload.message.strip()))

    @app.post("/api/chat/stream")
    async def chat_stream(payload: ChatRequest, user=Depends(current_user)):
        # Complete and persist first: errors remain proper HTTP errors, client disconnects
        # cannot leave an unpersisted half-response. This is response delivery, not token streaming.
        result = await chat(payload, user)
        data = result["data"]

        async def events():
            yield "data: " + dumps({"chunk": data["message"], "run_id": data["run_id"]}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/memory")
    def memory(conversation_id: str, user=Depends(current_user)):
        with app.state.store.connection() as db:
            row = app.state.store.owned(db, "conversations", conversation_id, user["id"])
            return ok(json.loads(row["memory"]))

    @app.post("/api/memory/clear")
    def clear_memory(payload: ConversationScope, user=Depends(current_user)):
        with app.state.store.connection(write=True) as db:
            row = app.state.store.owned(db, "conversations", payload.conversation_id, user["id"])
            if row["busy_until"] > int(time.time()):
                raise Problem("conversation_busy", "请等待当前请求完成后清空", 409)
            db.execute("UPDATE conversations SET memory='{}' WHERE id=?", (payload.conversation_id,))
            db.execute("DELETE FROM messages WHERE conversation_id=?", (payload.conversation_id,))
            app.state.store.audit(db, user["id"], "conversation.cleared", payload.conversation_id)
        return ok()

    @app.post("/api/memory/profile")
    def profile(payload: ProfileRequest, user=Depends(current_user)):
        if any(len(key) > 60 or len(value) > 200 for key, value in payload.profile.items()):
            raise Problem("invalid_profile", "偏好内容过长", 422)
        with app.state.store.connection(write=True) as db:
            row = app.state.store.owned(db, "conversations", payload.conversation_id, user["id"])
            if row["busy_until"] > int(time.time()):
                raise Problem("conversation_busy", "请等待当前请求完成", 409)
            data = json.loads(row["memory"])
            data["user_profile"] = payload.profile
            db.execute("UPDATE conversations SET memory=? WHERE id=?", (dumps(data), payload.conversation_id))
        return ok(payload.profile)

    @app.get("/api/runs")
    def runs(conversation_id: str, user=Depends(current_user)):
        with app.state.store.connection() as db:
            app.state.store.owned(db, "conversations", conversation_id, user["id"])
            rows = db.execute(
                "SELECT id,status,mode,duration_ms,created_at FROM runs WHERE user_id=? "
                "AND conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100",
                (user["id"], conversation_id),
            )
            return ok([dict(row) for row in rows])

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str, user=Depends(current_user)):
        with app.state.store.connection() as db:
            data = app.state.store.owned(db, "runs", run_id, user["id"])
            data["trace"] = json.loads(data["trace"])
            data.pop("user_id")
            return ok(data)

    @app.get("/api/health/dependencies")
    def dependency_health(user=Depends(current_user)):
        if app.state.mode == "demo":
            return ok({"application_storage": "ok", "external_services": "not_required", "mode": "demo"})
        from ..core.health import collect_dependency_health

        return ok(collect_dependency_health())

    @app.get("/api/plans")
    def plans(user=Depends(current_user)):
        with app.state.store.connection() as db:
            return ok(
                [
                    plan_data(row)
                    for row in db.execute(
                        "SELECT * FROM plans WHERE user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 300",
                        (user["id"],),
                    )
                ]
            )

    @app.post("/api/plans", status_code=201)
    def new_plan(payload: TripRequest, user=Depends(current_user)):
        app.state.store.limit("plan:" + user["id"], 30)
        return ok(app.state.booking.save_plan(user["id"], payload))

    @app.get("/api/plans/{plan_id}")
    def plan_detail(plan_id: str, user=Depends(current_user)):
        with app.state.store.connection() as db:
            return ok(plan_data(app.state.store.owned(db, "plans", plan_id, user["id"])))

    @app.put("/api/plans/{plan_id}")
    def edit_plan(plan_id: str, payload: PlanUpdate, user=Depends(current_user)):
        return ok(
            app.state.booking.save_plan(
                user["id"], TripRequest(**payload.model_dump(exclude={"version"})), plan_id, payload.version
            )
        )

    @app.get("/api/plans/{plan_id}/export")
    def export_plan(plan_id: str, user=Depends(current_user)):
        with app.state.store.connection() as db:
            plan = plan_data(app.state.store.owned(db, "plans", plan_id, user["id"]))
        return Response(
            plan_markdown(plan),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{plan_id}.md"'},
        )

    @app.post("/api/orders", status_code=201)
    def new_order(payload: OrderRequest, request: Request, user=Depends(current_user)):
        return ok(
            app.state.booking.create_order(
                user["id"], payload.plan_id, payload.version, request.headers.get("Idempotency-Key"), payload.confirmed
            )
        )

    @app.get("/api/orders")
    def orders(user=Depends(current_user)):
        app.state.booking.expire_orders()
        with app.state.store.connection() as db:
            return ok(
                [
                    order_data(row)
                    for row in db.execute(
                        "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 300",
                        (user["id"],),
                    )
                ]
            )

    @app.get("/api/orders/{order_id}")
    def order_detail(order_id: str, user=Depends(current_user)):
        app.state.booking.expire_orders()
        with app.state.store.connection() as db:
            return ok(order_data(app.state.store.owned(db, "orders", order_id, user["id"])))

    @app.post("/api/orders/{order_id}/pay")
    def pay(order_id: str, payload: Confirmation, user=Depends(current_user)):
        if not payload.confirmed:
            raise Problem("confirmation_required", "请确认模拟支付金额", 422)
        return ok(app.state.booking.transition(user["id"], order_id, "pay"))

    @app.post("/api/orders/{order_id}/cancel")
    def cancel(order_id: str, payload: Confirmation, user=Depends(current_user)):
        if not payload.confirmed:
            raise Problem("confirmation_required", "请确认取消或模拟退款", 422)
        return ok(app.state.booking.transition(user["id"], order_id, "cancel"))

    @app.get("/api/audit")
    def audit(user=Depends(current_user)):
        with app.state.store.connection() as db:
            return ok(
                [
                    dict(row)
                    for row in db.execute(
                        "SELECT action,object_id,details,created_at FROM audit_events "
                        "WHERE user_id=? ORDER BY id DESC LIMIT 100",
                        (user["id"],),
                    )
                ]
            )

    return app


# Initialize lazily when imported by uvicorn or the module entrypoint, not on every
# package import. Application DB is a dedicated file, never the supplier MySQL DB.
app = create_app()


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(
        app, host=os.getenv("SMARTVOYAGE_API_HOST", "127.0.0.1"), port=int(os.getenv("SMARTVOYAGE_API_PORT", "8080"))
    )


if __name__ == "__main__":
    run()
