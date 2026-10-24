"""Business acceptance tests against the real API and transactional SQLite storage.

No supplier credentials, payment network or MySQL are used. Concurrency tests
use separate database connections, not mocked inventory functions.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ..api.server import create_app
from ..product.booking import BookingService
from ..product.planning import TripRequest, local_today
from ..product.store import Problem, Store, verify_password


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTVOYAGE_MODE", "demo")
    return create_app(tmp_path / "app.sqlite3")


def account(app, email="alice@example.test"):
    client = TestClient(app)
    response = client.post("/api/auth/register", json={"email": email, "password": "DemoPass123!", "name": "旅行者"})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    client.headers["X-CSRF-Token"] = data["csrf_token"]
    return client, data["user"]["id"]


def trip(**changes):
    return {
        "origin": "北京",
        "destination": "成都",
        "departure_date": str(local_today() + timedelta(days=7)),
        "days": 3,
        "travelers": 1,
        "budget_cents": 300000,
        **changes,
    }


def plan(client, **changes):
    response = client.post("/api/plans", json=trip(**changes))
    assert response.status_code == 201, response.text
    return response.json()["data"]


def order(client, p, key="request-00001"):
    return client.post(
        "/api/orders",
        json={"plan_id": p["id"], "version": p["version"], "confirmed": True},
        headers={"Idempotency-Key": key},
    )


def test_registration_hash_cookie_and_revocation(app):
    client, user_id = account(app)
    cookie = client.cookies.get("smartvoyage_session")
    with app.state.store.connection() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        assert row["password_hash"] != "DemoPass123!"
        assert verify_password("DemoPass123!", row["password_hash"])
        assert cookie not in db.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.set("smartvoyage_session", cookie)
    assert client.get("/api/auth/me").status_code == 401


def test_login_wrong_password_and_duplicate(app):
    client, _ = account(app)
    bad = client.post("/api/auth/login", json={"email": "alice@example.test", "password": "wrongpass"})
    assert bad.status_code == 401
    assert (
        client.post(
            "/api/auth/register", json={"email": "ALICE@example.test", "password": "DemoPass123!", "name": "X"}
        ).status_code
        == 409
    )
    assert (
        client.post("/api/auth/login", json={"email": "alice@example.test", "password": "DemoPass123!"}).status_code
        == 200
    )


def test_authentication_csrf_and_validation_privacy(app):
    assert TestClient(app).get("/api/plans").status_code == 401
    client, _ = account(app)
    saved = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/plans", json=trip()).status_code == 403
    client.headers["X-CSRF-Token"] = saved
    response = client.post("/api/auth/login", json={"email": "bad", "password": "secret"})
    assert response.status_code == 422
    assert "secret" not in response.text
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "a@b.test", "password": "12345678"},
            headers={"Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )


def test_request_size_and_security_headers(app):
    client = TestClient(app)
    response = client.post("/api/auth/login", content="x" * 70000)
    assert response.status_code == 413
    response = client.get("/")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert client.get("/api/config").headers["cache-control"] == "no-store"


def test_rate_limit_survives_app_restart(app):
    client, _ = account(app)
    for _ in range(11):
        assert (
            client.post("/api/auth/login", json={"email": "absent@example.test", "password": "wrongpass"}).status_code
            == 401
        )
    other_app = create_app(app.state.store.path)
    assert (
        TestClient(other_app)
        .post("/api/auth/login", json={"email": "absent@example.test", "password": "wrongpass"})
        .status_code
        == 429
    )


def test_quote_components_and_budget(app):
    client, _ = account(app)
    p = plan(client, travelers=3, budget_cents=1000000)
    q = p["quote"]
    assert q["simulation"] is True
    assert q["amount_cents"] == 2 * 55000 * 3 + 2 * 24000 * 2 + 3000 * 3
    assert q["local_estimate_cents"] == 3 * 3 * 12000
    assert q["estimated_total_cents"] == q["amount_cents"] + q["local_estimate_cents"]
    assert len(q["itinerary"]) == 3
    assert client.post("/api/plans", json=trip(budget_cents=10000)).json()["code"] == "budget_exceeded"


@pytest.mark.parametrize(
    "changes",
    [
        {"days": 1},
        {"days": 8},
        {"travelers": 0},
        {"travelers": 7},
        {"destination": "不存在的城市"},
        {"departure_date": "2020-01-01"},
        {"transport": "boat"},
        {"budget_cents": -1},
        {"amount_cents": 1},
    ],
)
def test_trip_validation(app, changes):
    client, _ = account(app)
    assert client.post("/api/plans", json=trip(**changes)).status_code == 422


def test_edit_invalidates_previous_quote_and_booking_freezes_plan(app):
    client, _ = account(app)
    p = plan(client)
    update = client.put(f"/api/plans/{p['id']}", json={**trip(days=4), "version": 1})
    assert update.status_code == 200
    edited = update.json()["data"]
    assert edited["version"] == 2
    assert order(client, p).json()["code"] == "stale_plan"
    assert order(client, edited).status_code == 201
    assert client.put(f"/api/plans/{p['id']}", json={**trip(), "version": 2}).json()["code"] == "plan_booked"


def test_order_idempotency_and_confirmation(app):
    client, _ = account(app)
    p = plan(client)
    payload = {"plan_id": p["id"], "version": 1, "confirmed": False}
    assert client.post("/api/orders", json=payload, headers={"Idempotency-Key": "example-key"}).status_code == 422
    assert client.post("/api/orders", json={**payload, "confirmed": True}).status_code == 422
    first = order(client, p).json()["data"]
    second = order(client, p).json()["data"]
    third = order(client, p, "another-request").json()["data"]
    assert first["id"] == second["id"] == third["id"]
    other = plan(client, destination="杭州")
    assert order(client, other).json()["code"] == "idempotency_conflict"
    with app.state.store.connection() as db:
        available = db.execute("SELECT available FROM inventory WHERE product_id='train:北京:成都'").fetchone()[0]
        assert available == 19
    assert len(client.get("/api/orders").json()["data"]) == 1


def test_payment_refund_retries_restore_inventory_once(app):
    client, _ = account(app)
    o = order(client, plan(client)).json()["data"]
    endpoint = f"/api/orders/{o['id']}"
    assert client.post(endpoint + "/pay", json={"confirmed": False}).status_code == 422
    for _ in range(2):
        assert client.post(endpoint + "/pay", json={"confirmed": True}).json()["data"]["status"] == "confirmed"
    for _ in range(2):
        assert client.post(endpoint + "/cancel", json={"confirmed": True}).json()["data"]["status"] == "refunded"
    assert client.post(endpoint + "/pay", json={"confirmed": True}).status_code == 409
    with app.state.store.connection() as db:
        assert db.execute("SELECT available FROM inventory WHERE product_id='train:北京:成都'").fetchone()[0] == 20
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='order.refunded'").fetchone()[0] == 1


def test_cancel_unpaid_and_quote_expiration(app):
    client, _ = account(app)
    p = plan(client)
    with app.state.store.connection(write=True) as db:
        db.execute("UPDATE plans SET expires_at=0 WHERE id=?", (p["id"],))
    assert order(client, p).json()["code"] == "quote_expired"
    o = order(client, plan(client), "second-request").json()["data"]
    assert (
        client.post(f"/api/orders/{o['id']}/cancel", json={"confirmed": True}).json()["data"]["status"] == "cancelled"
    )


def test_expired_order_cannot_pay_and_releases_hold(app):
    client, _ = account(app)
    o = order(client, plan(client)).json()["data"]
    with app.state.store.connection(write=True) as db:
        db.execute("UPDATE orders SET expires_at=0 WHERE id=?", (o["id"],))
    assert client.post(f"/api/orders/{o['id']}/pay", json={"confirmed": True}).status_code == 409
    assert client.get(f"/api/orders/{o['id']}").json()["data"]["status"] == "expired"
    app.state.booking.expire_orders()
    with app.state.store.connection() as db:
        assert db.execute("SELECT available FROM inventory WHERE product_id='train:北京:成都'").fetchone()[0] == 20


def test_owner_isolation_for_every_resource(app):
    alice, _ = account(app)
    bob, _ = account(app, "bob@example.test")
    p = plan(alice)
    o = order(alice, p).json()["data"]
    cid = alice.post("/api/conversations", json={"title": "Alice only"}).json()["data"]["id"]
    run = alice.post("/api/chat", json={"conversation_id": cid, "message": "我的方案"}).json()["data"]["run_id"]
    for path in [
        f"/api/plans/{p['id']}",
        f"/api/orders/{o['id']}",
        f"/api/conversations/{cid}/messages",
        f"/api/memory?conversation_id={cid}",
        f"/api/runs?conversation_id={cid}",
        f"/api/runs/{run}",
    ]:
        assert bob.get(path).status_code == 404, path
    assert bob.put(f"/api/plans/{p['id']}", json={**trip(), "version": 1}).status_code == 404
    assert order(bob, p).status_code == 404
    for action in ["pay", "cancel"]:
        assert bob.post(f"/api/orders/{o['id']}/{action}", json={"confirmed": True}).status_code == 404
    assert bob.post("/api/chat", json={"conversation_id": cid, "message": "read"}).status_code == 404
    assert bob.post("/api/memory/clear", json={"conversation_id": cid}).status_code == 404
    assert bob.post("/api/memory/profile", json={"conversation_id": cid, "profile": {}}).status_code == 404
    for path in ["/api/conversations", "/api/plans", "/api/orders"]:
        assert bob.get(path).json()["data"] == []
    assert all(event["object_id"] not in {p["id"], o["id"], run} for event in bob.get("/api/audit").json()["data"])


def test_conversations_clear_and_run_history_persist(app):
    client, user_id = account(app)
    cids = [client.post("/api/conversations", json={"title": str(i)}).json()["data"]["id"] for i in range(2)]
    for cid, word in zip(cids, ["独立消息A", "独立消息B"], strict=True):
        result = client.post("/api/chat", json={"conversation_id": cid, "message": word})
        assert result.status_code == 200, result.text
        assert result.json()["data"]["trace"]["external_calls"] == 0
    assert client.get(f"/api/conversations/{cids[0]}/messages").json()["data"][0]["content"] == "独立消息A"
    assert client.post("/api/memory/clear", json={"conversation_id": cids[0]}).status_code == 200
    assert client.get(f"/api/conversations/{cids[0]}/messages").json()["data"] == []
    assert len(client.get(f"/api/conversations/{cids[1]}/messages").json()["data"]) == 2
    assert len(client.get(f"/api/runs?conversation_id={cids[0]}").json()["data"]) == 1
    restarted = Store(app.state.store.path)
    with restarted.connection() as db:
        assert (
            json.loads(restarted.owned(db, "conversations", cids[1], user_id)["memory"])["short_term_messages"][0][
                "content"
            ]
            == "独立消息B"
        )


def test_conversation_lease_blocks_concurrent_writes(app):
    client, user_id = account(app)
    cid = client.post("/api/conversations", json={}).json()["data"]["id"]
    _, lease = app.state.store.acquire_conversation(cid, user_id)
    assert client.post("/api/chat", json={"conversation_id": cid, "message": "hi"}).status_code == 409
    assert client.post("/api/memory/clear", json={"conversation_id": cid}).status_code == 409
    app.state.store.release_conversation(cid, "wrong-lease")
    assert client.post("/api/chat", json={"conversation_id": cid, "message": "hi"}).status_code == 409
    app.state.store.release_conversation(cid, lease)
    assert client.post("/api/chat", json={"conversation_id": cid, "message": "hi"}).status_code == 200


def test_no_overselling_across_concurrent_connections(app):
    _, user_id = account(app)
    booking = app.state.booking
    plans = [booking.save_plan(user_id, TripRequest(**trip(travelers=6, budget_cents=3000000))) for _ in range(4)]

    def book(p):
        try:
            # Independent repository instance models another API worker/process.
            return BookingService(Store(app.state.store.path)).create_order(user_id, p["id"], 1, p["id"], True)[
                "status"
            ]
        except Problem as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(book, plans))
    assert results.count("pending_payment") == 3
    assert results.count("sold_out") == 1
    with app.state.store.connection() as db:
        assert db.execute("SELECT min(available) FROM inventory").fetchone()[0] >= 0
        assert db.execute("SELECT available FROM inventory WHERE product_id='train:北京:成都'").fetchone()[0] == 2


def test_partial_reservation_rolls_back(app):
    _, user_id = account(app)
    p = app.state.booking.save_plan(user_id, TripRequest(**trip()))
    with app.state.store.connection(write=True) as db:
        # Fail at the hotel after both transport inventory writes have succeeded.
        db.execute("INSERT INTO inventory VALUES(?,?,0)", ("hotel:成都", trip()["departure_date"]))
    with pytest.raises(Problem, match="库存不足"):
        app.state.booking.create_order(user_id, p["id"], 1, "rollback-test", True)
    with app.state.store.connection() as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM inventory WHERE product_id LIKE 'train:%'").fetchone()[0] == 0


def test_live_failure_is_explicit_and_isolated(app, monkeypatch):
    client, _ = account(app)
    cid = client.post("/api/conversations", json={}).json()["data"]["id"]
    monkeypatch.setenv("SMARTVOYAGE_MODE", "live")

    def failing(*args):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("ai01_SmartVoyage.product.chat.live_reply", failing)
    assert client.post("/api/chat", json={"conversation_id": cid, "message": "hi"}).status_code == 503
    runs = client.get(f"/api/runs?conversation_id={cid}").json()["data"]
    assert runs[0]["status"] == "failed" and runs[0]["mode"] == "live"
    with app.state.store.connection() as db:
        assert db.execute("SELECT busy_until FROM conversations WHERE id=?", (cid,)).fetchone()[0] == 0


def test_demo_chat_never_claims_real_weather_or_purchase(app):
    client, _ = account(app)
    cid = client.post("/api/conversations", json={}).json()["data"]["id"]
    for message, expected in [("帮我查天气", "不能提供真实预报"), ("帮我订票", "聊天不会替你下单")]:
        result = client.post("/api/chat", json={"conversation_id": cid, "message": message}).json()["data"]
        assert expected in result["message"]


def test_expiration_sweep_on_startup(app):
    client, _ = account(app)
    o = order(client, plan(client)).json()["data"]
    with app.state.store.connection(write=True) as db:
        db.execute("UPDATE orders SET expires_at=0 WHERE id=?", (o["id"],))
    with TestClient(app):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with app.state.store.connection() as db:
                status = db.execute("SELECT status FROM orders WHERE id=?", (o["id"],)).fetchone()[0]
            if status == "expired":
                break
            time.sleep(0.02)
        assert status == "expired"


def test_concurrent_duplicate_requests_reserve_once(app):
    _, user_id = account(app)
    p = app.state.booking.save_plan(user_id, TripRequest(**trip()))

    def submit(_):
        booking = BookingService(Store(app.state.store.path))
        return booking.create_order(user_id, p["id"], 1, "same-concurrent-key", True)["id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit, range(4)))
    assert len(set(ids)) == 1
    with app.state.store.connection() as db:
        assert db.execute("SELECT available FROM inventory WHERE product_id='train:北京:成都'").fetchone()[0] == 19


def test_concurrent_payment_and_cancel_never_double_release(app):
    client, user_id = account(app)
    o = order(client, plan(client)).json()["data"]

    def transition(action):
        try:
            return BookingService(Store(app.state.store.path)).transition(user_id, o["id"], action)["status"]
        except Problem as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(transition, ["pay", "cancel"]))
    with app.state.store.connection() as db:
        assert db.execute("SELECT status FROM orders WHERE id=?", (o["id"],)).fetchone()[0] in {"cancelled", "refunded"}
        assert db.execute("SELECT available FROM inventory WHERE product_id='train:北京:成都'").fetchone()[0] == 20


def test_supplier_price_change_requires_requote(app, monkeypatch):
    from ..product.planning import ROUTES

    client, _ = account(app)
    p = plan(client)
    monkeypatch.setitem(ROUTES, ("北京", "成都"), (60000, 85000))
    assert order(client, p).json()["code"] == "price_changed"
    assert client.get("/api/orders").json()["data"] == []


def test_export_uses_saved_snapshot_and_checks_owner(app):
    client, _ = account(app)
    p = plan(client)
    response = client.get(f"/api/plans/{p['id']}/export")
    assert response.status_code == 200
    assert "1610.00" in response.text and "模拟" in response.text
    assert response.headers["content-disposition"].startswith("attachment;")
    other, _ = account(app, "bob@example.test")
    assert other.get(f"/api/plans/{p['id']}/export").status_code == 404


def test_live_success_persists_private_context(app, monkeypatch):
    client, _ = account(app)
    cid = client.post("/api/conversations", json={}).json()["data"]["id"]
    client.post("/api/memory/profile", json={"conversation_id": cid, "profile": {"seat": "二等座"}})
    monkeypatch.setenv("SMARTVOYAGE_MODE", "live")

    def fake_live(message, memory):
        assert memory["user_profile"] == {"seat": "二等座"}
        memory["short_term_messages"] = [{"role": "user", "content": message}]
        return "controlled live adapter result", {"status": "done", "mode": "live"}, memory

    monkeypatch.setattr("ai01_SmartVoyage.product.chat.live_reply", fake_live)
    response = client.post("/api/chat", json={"conversation_id": cid, "message": "private input"})
    assert response.status_code == 200
    assert response.json()["data"]["mode"] == "live"
    assert (
        client.get(f"/api/memory?conversation_id={cid}").json()["data"]["short_term_messages"][0]["content"]
        == "private input"
    )


def test_timeout_keeps_worker_slot_and_discards_late_result(app, monkeypatch):
    import threading

    from ..product import chat

    client, _ = account(app)
    cids = [client.post("/api/conversations", json={}).json()["data"]["id"] for _ in range(2)]
    finish = threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setenv("SMARTVOYAGE_MODE", "live")
    monkeypatch.setattr(chat, "LIVE_TIMEOUT", 0.05)
    monkeypatch.setattr(chat, "_slots", slots)

    def delayed(*args):
        finish.wait(3)
        return "LATE RESULT", {"status": "done"}, {"late": True}

    monkeypatch.setattr(chat, "live_reply", delayed)
    try:
        assert client.post("/api/chat", json={"conversation_id": cids[0], "message": "first"}).status_code == 503
        assert not slots.acquire(blocking=False)
        assert client.post("/api/chat", json={"conversation_id": cids[1], "message": "second"}).status_code == 503
    finally:
        finish.set()
    assert slots.acquire(timeout=2)
    slots.release()
    assert client.get(f"/api/memory?conversation_id={cids[0]}").json()["data"] == {}
    assert "LATE RESULT" not in client.get(f"/api/conversations/{cids[0]}/messages").text


def test_sse_response_persisted_once(app):
    client, _ = account(app)
    cid = client.post("/api/conversations", json={}).json()["data"]["id"]
    response = client.post("/api/chat/stream", json={"conversation_id": cid, "message": "你好"})
    assert response.status_code == 200 and "data: [DONE]" in response.text
    assert len(client.get(f"/api/conversations/{cid}/messages").json()["data"]) == 2


def test_online_backup_contains_committed_data(app, monkeypatch):
    import sqlite3
    from pathlib import Path

    from ..scripts.backup_data import main

    _, user_id = account(app)
    monkeypatch.setenv("SMARTVOYAGE_APP_DB", app.state.store.path)
    main()
    backups = list((Path(app.state.store.path).parent / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT id FROM users").fetchone()[0] == user_id
