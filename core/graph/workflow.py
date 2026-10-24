"""SmartVoyage 主 LangGraph 工作流。

这张图负责回答一个核心问题：
“一次用户请求应该经过哪些节点，最后如何形成可追踪的回复？”

主路径：

    START
      -> intent
      -> route
         -> follow_up -> finalize
         -> collaborative -> finalize
         -> plan -> direct/react -> finalize
         -> error -> finalize
      -> END

业务能力仍然由 ChatService、A2A、MCP 和 CollaborationEngine 提供；
LangGraph 只负责状态流转和分支表达。
"""

import json
import uuid
from datetime import datetime
from typing import AsyncIterator, Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from ...infrastructure.logging import logger
from ..message_utils import query_to_text
from .state import SmartVoyageState


class SmartVoyageWorkflowGraph:
    """SmartVoyage 的主工作流图。"""

    def __init__(
        self,
        *,
        intent_agent: Callable[[str], tuple[list, dict, str]],
        should_use_collaborative_planning: Callable[[list, str], bool],
        collaborative_plan: Callable[..., Awaitable[str]],
        should_skip_planning: Callable[[list], bool],
        planning_agent: Callable[[list, dict], dict],
        react_loop: Callable[[list, dict], Awaitable[str]],
        call_agent_intent: Callable[[str, object], Awaitable[str]],
        record_user_message: Callable[[str], None],
        record_assistant_message: Callable[[str], None],
        record_error_message: Callable[[str], None],
        trace_recorder,
        intent_map: dict,
    ):
        """注入节点所需的业务能力，并在初始化时编译一次工作流图。"""
        self.intent_agent = intent_agent
        self.should_use_collaborative_planning = should_use_collaborative_planning
        self.collaborative_plan = collaborative_plan
        self.should_skip_planning = should_skip_planning
        self.planning_agent = planning_agent
        self.react_loop = react_loop
        self.call_agent_intent = call_agent_intent
        self.record_user_message = record_user_message
        self.record_assistant_message = record_assistant_message
        self.record_error_message = record_error_message
        self.trace_recorder = trace_recorder
        self.intent_map = intent_map
        self.graph = self._build_graph()

    def _build_initial_state(self, user_input: str) -> SmartVoyageState:
        """创建一次运行的初始状态。"""
        return {
            "user_input": user_input,
            "run_id": f"run-{uuid.uuid4()}",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "created",
            "mode": "standard",
            "route": "",
            "plan_route": "",
            "intents": [],
            "user_queries": {},
            "follow_up_message": "",
            "plan": {},
            "execution_trace": [],
            "response": "",
            "collaboration_used": False,
            "error_message": "",
        }

    @staticmethod
    def _error_update(stage: str, exc: Exception) -> dict:
        """把节点异常转换成统一的 error 状态，交给 error 节点收口。"""
        if stage == "intent" and isinstance(exc, json.JSONDecodeError):
            message = f"意图识别JSON解析失败：{exc}。请重试。"
        else:
            message = f"处理失败：{exc}。请重试。"

        logger.exception("LangGraph 节点失败: %s", stage)
        return {
            "mode": "error",
            "status": "failed",
            "route": "error",
            "plan_route": "error",
            "error_message": message,
            "response": message,
        }

    def _intent_node(self, state: SmartVoyageState) -> dict:
        """节点 1：识别用户意图并改写 Agent 查询。"""
        try:
            intents, user_queries, follow_up_message = self.intent_agent(
                state["user_input"]
            )
            return {
                "intents": intents,
                "user_queries": user_queries,
                "follow_up_message": follow_up_message,
                "status": "intent_recognized",
            }
        except Exception as exc:
            return self._error_update("intent", exc)

    def _route_node(self, state: SmartVoyageState) -> dict:
        """节点 2：根据意图和请求复杂度选择下一条图路径。"""
        try:
            if state.get("error_message"):
                route = "error"
            elif (
                "out_of_scope" in state.get("intents", [])
                or state.get("follow_up_message")
            ):
                route = "follow_up"
            elif self.should_use_collaborative_planning(
                state.get("intents", []),
                state["user_input"],
            ):
                route = "collaborative"
            else:
                route = "standard"

            mode = "follow_up" if route == "follow_up" else state.get("mode", "standard")
            if route == "collaborative":
                mode = "collaborative"

            return {
                "route": route,
                "mode": mode,
                "status": "dispatched" if route == "collaborative" else state.get(
                    "status", "intent_recognized"
                ),
                "collaboration_used": route == "collaborative",
            }
        except Exception as exc:
            return self._error_update("route", exc)

    def _plan_node(self, state: SmartVoyageState) -> dict:
        """节点 3：为普通请求生成 direct 或 ReAct 执行计划。"""
        try:
            if self.should_skip_planning(state.get("intents", [])):
                plan = {
                    "need_plan": False,
                    "reason": "启发式判断：任务简单，可直接执行",
                    "steps": [],
                }
                logger.info("跳过规划: %s", plan["reason"])
            else:
                plan = self.planning_agent(
                    state.get("intents", []),
                    state.get("user_queries", {}),
                )

            return {
                "plan": plan,
                "plan_route": "react" if plan.get("need_plan", False) else "direct",
                "status": "planned",
            }
        except Exception as exc:
            return self._error_update("plan", exc)

    async def _direct_node(self, state: SmartVoyageState) -> dict:
        """节点 4A：逐个调用 Agent，处理简单 direct 请求。"""
        try:
            responses = []
            execution_trace = list(state.get("execution_trace", []))

            for intent in state.get("intents", []):
                logger.info("处理意图: %s", intent)
                query = state.get("user_queries", {}).get(intent, {})
                result = await self.call_agent_intent(intent, query)
                responses.append(result)
                execution_trace.append({
                    "round": 1,
                    "intent": intent,
                    "agent": self.intent_map.get(intent, "AttractionGenerator"),
                    "query": query_to_text(query),
                    "result": result,
                })

            return {
                "mode": "direct",
                "status": "summarized",
                "response": "\n\n".join(responses),
                "execution_trace": execution_trace,
            }
        except Exception as exc:
            return self._error_update("direct", exc)

    async def _react_node(self, state: SmartVoyageState) -> dict:
        """节点 4B：执行普通规划步骤并汇总 ReAct 结果。"""
        try:
            steps = state.get("plan", {}).get("steps", [])
            response = await self.react_loop(
                steps,
                state.get("user_queries", {}),
            )
            execution_trace = list(state.get("execution_trace", []))
            for step in steps:
                intent = step.get("intent", "")
                execution_trace.append({
                    "round": 1,
                    "intent": intent,
                    "agent": self.intent_map.get(intent, "AttractionGenerator"),
                    "query": query_to_text(
                        state.get("user_queries", {}).get(intent, state["user_input"])
                    ),
                    "result": "已由 ReAct 汇总链路整合到最终回复",
                })

            return {
                "mode": "react",
                "status": "summarized",
                "response": response,
                "execution_trace": execution_trace,
            }
        except Exception as exc:
            return self._error_update("react", exc)

    async def _collaborative_node(self, state: SmartVoyageState) -> dict:
        """节点 4C：进入 Planner/Expert/Critic/Summary 协作链。"""
        try:
            run_meta = {
                "run_id": state["run_id"],
                "status": "done",
                "mode": "collaborative",
                "created_at": state["created_at"],
            }
            response = await self.collaborative_plan(
                state["user_input"],
                state.get("intents", []),
                state.get("user_queries", {}),
                run_meta=run_meta,
            )
            return {
                "mode": "collaborative",
                "status": "summarized",
                "collaboration_used": True,
                "response": response,
            }
        except Exception as exc:
            return self._error_update("collaborative", exc)

    @staticmethod
    def _follow_up_node(state: SmartVoyageState) -> dict:
        """节点 4D：信息不足或越界时直接返回追问。"""
        return {
            "mode": "follow_up",
            "status": "summarized",
            "response": state.get("follow_up_message")
            or "请补充目的地、日期或具体旅行需求。",
        }

    @staticmethod
    def _error_node(state: SmartVoyageState) -> dict:
        """节点 4E：统一生成错误回复。"""
        return {
            "mode": "error",
            "status": "failed",
            "response": state.get("error_message") or "处理失败，请稍后重试。",
        }

    def _finalize_node(self, state: SmartVoyageState) -> dict:
        """节点 5：写入 trace 和记忆，完成一次工作流运行。"""
        is_error = state.get("mode") == "error"
        final_status = "failed" if is_error else "done"
        response = state.get("response", "") or "暂无结果"
        run_meta = {
            "run_id": state["run_id"],
            "status": final_status,
            "mode": state.get("mode", "standard"),
            "created_at": state["created_at"],
        }

        if is_error:
            self.trace_recorder.record_error(
                state["user_input"],
                response,
                run_meta=run_meta,
            )
            self.record_error_message(response)
        else:
            if not state.get("collaboration_used"):
                plan = state.get("plan", {})
                self.trace_recorder.record_standard(
                    state.get("mode", "standard"),
                    state["user_input"],
                    state.get("intents", []),
                    state.get("user_queries", {}),
                    plan=plan,
                    execution=state.get("execution_trace", []),
                    reason=plan.get("reason", state.get("follow_up_message", "")),
                    run_meta=run_meta,
                )
            self.record_assistant_message(response)

        return {
            "status": final_status,
            "response": response,
        }

    @staticmethod
    def _route_key(state: SmartVoyageState) -> str:
        """为 route 节点返回 LangGraph conditional edge 的 key。"""
        return state.get("route", "error")

    @staticmethod
    def _plan_key(state: SmartVoyageState) -> str:
        """为 plan 节点返回 direct、react 或 error 分支。"""
        if state.get("error_message"):
            return "error"
        return "react" if state.get("plan", {}).get("need_plan") else "direct"

    def _build_graph(self):
        """构建并编译 SmartVoyage 主图。"""
        builder = StateGraph(SmartVoyageState)

        # 每个节点对应授课流程图中的一个明确步骤。
        builder.add_node("intent", self._intent_node)
        builder.add_node("route", self._route_node)
        builder.add_node("plan", self._plan_node)
        builder.add_node("direct", self._direct_node)
        builder.add_node("react", self._react_node)
        builder.add_node("collaborative", self._collaborative_node)
        builder.add_node("follow_up", self._follow_up_node)
        builder.add_node("error", self._error_node)
        builder.add_node("finalize", self._finalize_node)

        builder.add_edge(START, "intent")
        builder.add_edge("intent", "route")
        builder.add_conditional_edges(
            "route",
            self._route_key,
            {
                "follow_up": "follow_up",
                "collaborative": "collaborative",
                "standard": "plan",
                "error": "error",
            },
        )
        builder.add_conditional_edges(
            "plan",
            self._plan_key,
            {
                "direct": "direct",
                "react": "react",
                "error": "error",
            },
        )

        for node_name in ("follow_up", "direct", "react", "collaborative", "error"):
            builder.add_edge(node_name, "finalize")

        builder.add_edge("finalize", END)
        return builder.compile()

    async def chat(self, user_input: str) -> str:
        """运行 LangGraph，并返回最终用户文本。"""
        # 用户消息必须在意图识别前写入记忆，后续节点才能读取完整上下文。
        self.record_user_message(user_input)
        result = await self.graph.ainvoke(self._build_initial_state(user_input))
        return result.get("response", "暂无结果")

    async def chat_stream(self, user_input: str) -> AsyncIterator[str]:
        """以 SSE 兼容方式返回结果。

        图统一负责状态推进；当前以完整回复作为一个 chunk 输出，避免同时维护
        一套旧的流式状态机。后续如果需要 token 级流式，可在具体节点接入
        LangGraph 的 stream_mode="messages"。
        """
        yield await self.chat(user_input)
