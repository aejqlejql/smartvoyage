"""Per-request Agent instances, durable per-conversation context and historical runs."""

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .store import Problem, dumps, uid

LIVE_TIMEOUT = 100
_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="smartvoyage-agent")
_slots = threading.BoundedSemaphore(4)


async def bounded_live_reply(message, memory):
    # A timeout keeps its capacity slot until the underlying thread really finishes.
    if not _slots.acquire(blocking=False):
        raise Problem("agent_busy", "联网助手正在处理其他请求，请稍后重试", 503)
    slots = _slots
    try:
        future = _workers.submit(live_reply, message, memory)
    except Exception:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout=LIVE_TIMEOUT)


def mode():
    selected = os.getenv("SMARTVOYAGE_MODE", "demo").lower()
    if selected not in {"demo", "live"}:
        raise ValueError("SMARTVOYAGE_MODE must be demo or live")
    return selected


def live_reply(message, memory):
    # The old MySQL memory tables are single-user. Never attach them to this service.
    # A fresh service also prevents cancellation or simultaneous runs from sharing mutable state.
    from ..app.chat.service import ChatService
    from ..app.memory.conversation import ConversationMemory

    service = ChatService(persistent_memory=False)
    service.memory = ConversationMemory.from_dict(memory)
    service.collaboration_engine.memory = service.memory
    service.messages = [
        {"role": item["role"], "content": item["content"]} for item in service.memory.short_term_messages
    ]
    response = asyncio.run(service.chat(message))
    return response, service.get_collaboration_trace(), service.memory.to_dict()


def demo_reply(message, memory, plans):
    """Rule-based walkthrough; intentionally does not claim to invoke an LLM or real agents."""
    if any(word in message for word in ("支付", "预订", "订票", "下单", "退款", "取消")):
        reply = "请在「行程方案」中确认报价并创建模拟订单，再到「订单中心」模拟支付或取消退款。聊天不会替你下单或扣款。"
    elif "天气" in message:
        reply = (
            "演示模式未接入实时天气，不能提供真实预报。请在出行前核实天气；联网 Agent 模式可以调用已配置的天气服务。"
        )
    elif plans:
        plan = plans[0]
        quote = json.loads(plan["quote"])
        reply = (
            f"你最近保存的方案是：{plan['title']}。\n"
            f"模拟预订项 ¥{quote['amount_cents'] / 100:.2f}，餐饮及市内交通预估 "
            f"¥{quote['local_estimate_cents'] / 100:.2f}，合计预估 ¥{quote['estimated_total_cents'] / 100:.2f}。\n"
            "在「行程方案」中可修改日期、人数和预算，系统会重新计算；确认后才能创建模拟订单。\n"
            "当前回复来自规则演示，价格和库存均为示例，未调用大模型或真实供应商。"
        )
    else:
        reply = (
            "先到「行程方案」选择出发地、目的地、日期、人数和预算，生成可编辑的旅行方案。\n"
            "演示支持北京、上海、广州出发，前往成都、杭州、西安；可选择高铁或飞机，包含往返交通和住宿。\n"
            "当前是规则演示模式，无需模型密钥；所有预订及支付均为模拟。"
        )
    messages = memory.get("short_term_messages", []) + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": reply},
    ]
    memory["short_term_messages"] = messages[-10:]
    trace = {
        "mode": "demo",
        "status": "done",
        "engine": "deterministic_rules",
        "steps": [{"name": "上下文读取", "status": "success"}, {"name": "规则回复", "status": "success"}],
        "external_calls": 0,
        "note": "未调用 LLM、A2A 或 MCP；不代表真实多智能体执行",
    }
    return reply, trace, memory


async def execute_chat(store, user_id, conversation_id, message):
    selected = mode()
    conversation, lease = store.acquire_conversation(conversation_id, user_id)
    start, run_id = time.monotonic(), uid("run")
    error = None
    try:
        memory = json.loads(conversation["memory"])
        if selected == "live":
            # All legacy synchronous LLM/network calls stay off the API event loop.
            # A timed-out worker only owns local memory and cannot persist a late reply.
            reply, trace, memory = await bounded_live_reply(message, memory)
            status = "failed" if trace.get("status") == "failed" else "done"
        else:
            with store.connection() as db:
                plans = db.execute(
                    "SELECT * FROM plans WHERE user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (user_id,)
                ).fetchall()
            reply, trace, memory = demo_reply(message, memory, plans)
            status = "done"
    except asyncio.CancelledError:
        store.release_conversation(conversation_id, lease)
        raise
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception("Chat failed: %s", run_id)
        error = Problem(
            "agent_unavailable", "联网 Agent 暂时不可用或请求超时。请检查模型、A2A/MCP服务及运行日志。", 503
        )
        reply = error.message
        trace = {"status": "failed", "error_type": type(exc).__name__, "mode": selected}
        status, memory = "failed", json.loads(conversation["memory"])
    duration = round((time.monotonic() - start) * 1000)
    trace.update({"run_id": run_id, "mode": selected})
    try:
        with store.connection(write=True) as db:
            current = store.owned(db, "conversations", conversation_id, user_id)
            if current["lease"] != lease:
                raise Problem("lease_expired", "该请求已失效，请刷新会话", 409)
            now = int(time.time())
            for role, text in [("user", message), ("assistant", reply)]:
                db.execute(
                    "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
                    (conversation_id, role, text, now),
                )
            db.execute("UPDATE conversations SET memory=? WHERE id=?", (dumps(memory), conversation_id))
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                (run_id, user_id, conversation_id, status, selected, dumps(trace), duration, now),
            )
            store.audit(
                db,
                user_id,
                "chat.completed" if status == "done" else "chat.failed",
                run_id,
                {"mode": selected, "duration_ms": duration},
            )
    finally:
        store.release_conversation(conversation_id, lease)
    if error:
        raise error
    return {"message": reply, "run_id": run_id, "trace": trace, "mode": selected, "duration_ms": duration}
