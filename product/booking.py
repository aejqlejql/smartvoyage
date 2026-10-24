"""Transactional quotes, inventory holds and sandbox order state machine."""

import hashlib
import json
import secrets
import time

from .planning import TripRequest, build_quote
from .store import Problem, Store, dumps, uid


def plan_data(row):
    data = dict(row)
    data["request"] = json.loads(data["request"])
    data["quote"] = json.loads(data["quote"])
    data["expired"] = data["expires_at"] <= int(time.time())
    data.pop("user_id", None)
    return data


def order_data(row):
    data = dict(row)
    data["quote"] = json.loads(data["quote"])
    data["simulation"] = True
    data.pop("user_id", None)
    return data


class BookingService:
    def __init__(self, store: Store):
        self.store = store

    def save_plan(self, user_id, request: TripRequest, plan_id=None, version=None):
        quote = build_quote(request)
        now = int(time.time())
        with self.store.connection(write=True) as db:
            if plan_id:
                old = self.store.owned(db, "plans", plan_id, user_id)
                if old["version"] != version:
                    raise Problem("stale_plan", "方案已更新，请刷新后再编辑", 409)
                if db.execute("SELECT 1 FROM orders WHERE plan_id=?", (plan_id,)).fetchone():
                    raise Problem("plan_booked", "该方案已有订单，请新建方案", 409)
                new_version = old["version"] + 1
            else:
                count = db.execute("SELECT count(*) FROM plans WHERE user_id=?", (user_id,)).fetchone()[0]
                if count >= 300:
                    raise Problem("plan_limit", "最多保留300个方案")
                plan_id, new_version = uid("plan"), 1
            title = f"{request.origin} → {request.destination} · {request.days}天{request.travelers}人"
            params = (user_id, title, dumps(request.model_dump(mode="json")), dumps(quote), new_version, now + 900)
            if new_version == 1:
                db.execute(
                    "INSERT INTO plans(id,user_id,title,request,quote,version,expires_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (plan_id, *params, now),
                )
            else:
                db.execute(
                    "UPDATE plans SET user_id=?,title=?,request=?,quote=?,version=?,expires_at=? WHERE id=?",
                    (*params, plan_id),
                )
            self.store.audit(
                db,
                user_id,
                "plan.created" if new_version == 1 else "plan.updated",
                plan_id,
                {"version": new_version, "source": quote["source"]},
            )
            return plan_data(self.store.owned(db, "plans", plan_id, user_id))

    def expire_orders(self):
        now = int(time.time())
        with self.store.connection(write=True) as db:
            expired = db.execute(
                "SELECT * FROM orders WHERE status='pending_payment' AND expires_at<=?", (now,)
            ).fetchall()
            for row in expired:
                self._release_stock(db, json.loads(row["quote"]))
                db.execute("UPDATE orders SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                self.store.audit(db, row["user_id"], "order.expired", row["id"])

    @staticmethod
    def _release_stock(db, quote):
        for item in quote["items"]:
            db.execute(
                "UPDATE inventory SET available=available+? WHERE product_id=? AND service_date=?",
                (item["quantity"], item["product_id"], item["service_date"]),
            )

    def create_order(self, user_id, plan_id, version, key, confirmed):
        if not confirmed:
            raise Problem("confirmation_required", "请先确认方案、金额及模拟预订说明", 422)
        if not key or not 8 <= len(key) <= 100:
            raise Problem("invalid_idempotency_key", "需要8至100字符的 Idempotency-Key", 422)
        fingerprint = hashlib.sha256(dumps([plan_id, version]).encode()).hexdigest()
        self.expire_orders()
        now = int(time.time())
        with self.store.connection(write=True) as db:
            previous = db.execute("SELECT * FROM idempotency WHERE user_id=? AND key=?", (user_id, key)).fetchone()
            if previous:
                if not secrets.compare_digest(previous["fingerprint"], fingerprint):
                    raise Problem("idempotency_conflict", "该请求编号已用于其他方案，请刷新重试", 409)
                return order_data(self.store.owned(db, "orders", previous["order_id"], user_id))
            plan = self.store.owned(db, "plans", plan_id, user_id)
            if plan["version"] != version:
                raise Problem("stale_plan", "方案版本已变化，请查看新报价后确认", 409)
            existing = db.execute("SELECT * FROM orders WHERE plan_id=? AND user_id=?", (plan_id, user_id)).fetchone()
            if existing:
                db.execute("INSERT INTO idempotency VALUES(?,?,?,?)", (user_id, key, fingerprint, existing["id"]))
                return order_data(existing)
            if plan["expires_at"] <= now:
                raise Problem("quote_expired", "报价已过期，请编辑并重新生成报价", 409)
            # Revalidate date and supplier prices inside the same booking transaction.
            request = TripRequest.model_validate_json(plan["request"])
            quote = json.loads(plan["quote"])
            fresh = build_quote(request)
            if fresh["items"] != quote["items"] or fresh["amount_cents"] != quote["amount_cents"]:
                raise Problem("price_changed", "演示目录报价已变化，请重新生成方案", 409)
            for item in quote["items"]:
                db.execute(
                    "INSERT OR IGNORE INTO inventory VALUES(?,?,?)",
                    (item["product_id"], item["service_date"], item["capacity"]),
                )
                update = db.execute(
                    "UPDATE inventory SET available=available-? WHERE product_id=? AND service_date=? AND available>=?",
                    (item["quantity"], item["product_id"], item["service_date"], item["quantity"]),
                )
                if update.rowcount != 1:
                    raise Problem("sold_out", f"{item['label']}在{item['service_date']}的模拟库存不足，请修改方案", 409)
            order_id = uid("order")
            db.execute(
                "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    order_id,
                    user_id,
                    plan_id,
                    dumps(quote),
                    quote["amount_cents"],
                    "pending_payment",
                    now + 900,
                    now,
                    now,
                ),
            )
            db.execute("INSERT INTO idempotency VALUES(?,?,?,?)", (user_id, key, fingerprint, order_id))
            self.store.audit(
                db,
                user_id,
                "order.created",
                order_id,
                {"amount_cents": quote["amount_cents"], "plan_version": version, "simulation": True},
            )
            return order_data(self.store.owned(db, "orders", order_id, user_id))

    def transition(self, user_id, order_id, action):
        self.expire_orders()
        now = int(time.time())
        with self.store.connection(write=True) as db:
            order = self.store.owned(db, "orders", order_id, user_id)
            status = order["status"]
            if action == "pay":
                if status == "confirmed":
                    return order_data(order)
                if status != "pending_payment":
                    raise Problem("invalid_order_state", "仅待支付且未过期的订单可以模拟支付", 409)
                new_status = "confirmed"
            elif action == "cancel":
                if status in {"cancelled", "refunded", "expired"}:
                    return order_data(order)
                if status not in {"pending_payment", "confirmed"}:
                    raise Problem("invalid_order_state", "该订单状态不支持取消", 409)
                new_status = "cancelled" if status == "pending_payment" else "refunded"
                self._release_stock(db, json.loads(order["quote"]))
            else:
                raise Problem("invalid_action", "不支持的订单操作")
            db.execute("UPDATE orders SET status=?,updated_at=? WHERE id=?", (new_status, now, order_id))
            self.store.audit(db, user_id, f"order.{new_status}", order_id, {"simulation": True, "from": status})
            return order_data(self.store.owned(db, "orders", order_id, user_id))
