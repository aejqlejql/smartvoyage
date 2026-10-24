"""SmartVoyage LangGraph 工作流测试。

测试只覆盖流程图，不连接真实 MySQL、A2A、MCP 或 LLM：

- direct 路径是否调用 Agent 并记录 trace。
- ReAct 路径是否保留 plan 和执行摘要。
- collaborative 路径是否把 run 元信息传给协作引擎。
- 节点异常是否统一进入 error trace。
"""

import json
import unittest

from ..core.graph.workflow import SmartVoyageWorkflowGraph
from ..core.trace_recorder import TraceRecorder


class TestSmartVoyageWorkflowGraph(unittest.IsolatedAsyncioTestCase):
    """围绕 LangGraph 主图的四条关键路径编写单元测试。"""

    def make_graph(
        self,
        *,
        intent_agent,
        should_use_collaborative_planning=None,
        collaborative_plan=None,
        should_skip_planning=None,
        planning_agent=None,
        react_loop=None,
        call_agent_intent=None,
    ):
        """创建带假依赖的测试工作流图。"""
        self.messages = []
        self.trace_recorder = TraceRecorder()

        async def default_collaborative_plan(
            user_input,
            intents,
            user_queries,
            run_meta=None,
        ):
            """假的协作引擎：记录 collaborative trace 并返回固定结果。"""
            self.trace_recorder.record_collaboration(
                user_input,
                {"agent_tasks": []},
                [],
                {"revision_suggestions": {}},
                "协作结果",
                run_meta=run_meta,
            )
            return "协作结果"

        async def default_call_agent_intent(intent, query):
            """假的 Agent 调用：把 intent 和 query 拼成可断言结果。"""
            return f"{intent}:{query}"

        async def default_react_loop(_steps, _queries):
            """假的 ReAct 汇总：避免测试触发真实 LLM。"""
            return "ReAct 汇总结果"

        return SmartVoyageWorkflowGraph(
            intent_agent=intent_agent,
            should_use_collaborative_planning=(
                should_use_collaborative_planning or (lambda _i, _q: False)
            ),
            collaborative_plan=collaborative_plan or default_collaborative_plan,
            should_skip_planning=should_skip_planning or (lambda _i: True),
            planning_agent=planning_agent or (lambda _i, _q: {"need_plan": False}),
            react_loop=react_loop or default_react_loop,
            call_agent_intent=call_agent_intent or default_call_agent_intent,
            record_user_message=lambda text: self.messages.append(("user", text)),
            record_assistant_message=lambda text: self.messages.append(("assistant", text)),
            record_error_message=lambda text: self.messages.append(("assistant", text)),
            trace_recorder=self.trace_recorder,
            intent_map={"weather": "WeatherQueryAssistant"},
        )

    async def test_direct_route_records_run_metadata(self):
        """direct 路径应返回 Agent 结果，并记录运行元信息和执行轨迹。"""
        graph = self.make_graph(
            intent_agent=lambda _q: (
                ["weather"],
                {"weather": "北京天气"},
                "",
            )
        )

        response = await graph.chat("查天气")
        trace = self.trace_recorder.get()

        self.assertEqual(response, "weather:北京天气")
        self.assertEqual(trace["mode"], "direct")
        self.assertEqual(trace["status"], "done")
        self.assertTrue(trace["run_id"].startswith("run-"))
        self.assertEqual(trace["execution"][0]["agent"], "WeatherQueryAssistant")
        self.assertEqual(self.messages[-1], ("assistant", "weather:北京天气"))

    async def test_react_route_records_plan_and_execution(self):
        """ReAct 路径应保留规划结果和执行摘要。"""
        graph = self.make_graph(
            intent_agent=lambda _q: (
                ["weather", "order"],
                {"weather": "北京天气"},
                "",
            ),
            should_skip_planning=lambda _i: False,
            planning_agent=lambda _i, _q: {
                "need_plan": True,
                "reason": "需要分步执行",
                "steps": [{"intent": "weather"}],
            },
        )

        response = await graph.chat("先查天气再规划")
        trace = self.trace_recorder.get()

        self.assertEqual(response, "ReAct 汇总结果")
        self.assertEqual(trace["mode"], "react")
        self.assertEqual(trace["planner"]["plan"]["reason"], "需要分步执行")
        self.assertIn("ReAct", trace["execution"][0]["result"])

    async def test_collaborative_route_passes_run_metadata(self):
        """协作路径应把 run 元信息传给协作引擎。"""
        graph = self.make_graph(
            intent_agent=lambda _q: (
                ["weather", "train", "insurance"],
                {"weather": "北京天气"},
                "",
            ),
            should_use_collaborative_planning=lambda _i, _q: True,
        )

        response = await graph.chat("规划一站式方案")
        trace = self.trace_recorder.get()

        self.assertEqual(response, "协作结果")
        self.assertEqual(trace["mode"], "collaborative")
        self.assertEqual(trace["status"], "done")
        self.assertTrue(trace["run_id"].startswith("run-"))

    async def test_json_decode_error_records_failed_trace(self):
        """意图识别 JSON 解析失败时应进入 error trace。"""
        def bad_intent_agent(_query):
            """模拟 LLM 返回不可解析 JSON 的意图识别器。"""
            raise json.JSONDecodeError("bad json", "x", 0)

        graph = self.make_graph(intent_agent=bad_intent_agent)

        response = await graph.chat("坏输入")
        trace = self.trace_recorder.get()

        self.assertIn("意图识别JSON解析失败", response)
        self.assertEqual(trace["mode"], "error")
        self.assertEqual(trace["status"], "failed")
        self.assertEqual(self.messages[-1][0], "assistant")


if __name__ == "__main__":
    unittest.main()
