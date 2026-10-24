"""SmartVoyage 执行轨迹记录器。

TraceRecorder 保存“最近一次请求”的结构化执行轨迹，供两个地方使用：

- 后端接口 ``/api/collaboration/trace`` 直接返回。
- 前端右侧 ``Collaboration Trace`` 面板渲染 Planner、Expert、Critic、Summary 状态。

当前实现只保存在内存中，适合教学演示和单会话调试；如果后续需要历史 trace，
可以把 ``self._trace`` 的结构按 ``run_id`` 落库或写入事件表。
"""


class TraceRecorder:
    """维护最近一次工作流运行的 trace 快照。"""

    def __init__(self):
        """初始化为空闲 trace，等待第一次对话运行写入。"""
        self._trace = self._idle_trace()

    @staticmethod
    def _idle_trace() -> dict:
        """返回空闲状态 trace，供初始化和清空记忆时复位。"""
        return {
            "mode": "idle",
            "run_id": "",
            "status": "idle",
            "created_at": "",
            "goal": "",
            "planner": {},
            "execution": [],
            "critic": {},
            "summary_agent": {},
        }

    def get(self) -> dict:
        """读取最近一次 trace，API 层会直接序列化该字典。"""
        return self._trace

    def reset(self):
        """清空当前 trace，通常在用户清空记忆时调用。"""
        self._trace = self._idle_trace()

    def record_standard(
        self,
        mode: str,
        user_input: str,
        intents: list,
        user_queries: dict,
        plan: dict | None = None,
        execution: list | None = None,
        reason: str = "",
        run_meta: dict | None = None,
    ):
        """记录普通链路 trace。

        普通链路包括 direct、ReAct、追问和 error。它们没有 CriticAgent，
        但仍需要展示 run 元信息、意图、查询、规划和执行结果。
        """
        run_meta = run_meta or {}
        self._trace = {
            "mode": mode,
            "run_id": run_meta.get("run_id", ""),
            "status": run_meta.get("status", "completed"),
            "created_at": run_meta.get("created_at", ""),
            "goal": user_input,
            "planner": {
                "agent": "ChatServiceRouter",
                "status": "completed",
                "intents": intents,
                "queries": user_queries,
                "plan": plan or {},
                "reason": reason,
            },
            "execution": execution or [],
            "critic": {},
            "summary_agent": {
                "agent": "ChatService",
                "status": "completed",
            },
        }

    def record_collaboration(
        self,
        user_input: str,
        collaboration_plan: dict,
        agent_results: list,
        critic_review: dict,
        summary: str,
        run_meta: dict | None = None,
    ):
        """记录强多智能体协作链路 trace。

        该结构与前端展示强绑定：planner、execution、critic、summary_agent 分别对应
        页面上的四个阶段。
        """
        run_meta = run_meta or {}
        self._trace = {
            "mode": "collaborative",
            "run_id": run_meta.get("run_id", ""),
            "status": run_meta.get("status", "completed"),
            "created_at": run_meta.get("created_at", ""),
            "goal": user_input,
            "planner": {
                "agent": "PlannerAgent",
                "status": "completed",
                "plan": collaboration_plan,
            },
            "execution": agent_results,
            "critic": {
                "agent": "CriticAgent",
                "status": "completed",
                "review": critic_review,
            },
            "summary_agent": {
                "agent": "SummaryAgent",
                "status": "completed",
                "summary_preview": summary[:500],
            },
        }

    def record_error(
        self,
        user_input: str,
        error_message: str,
        run_meta: dict | None = None,
    ):
        """记录错误 trace，并复用普通链路结构展示错误原因。"""
        self.record_standard(
            "error",
            user_input,
            [],
            {},
            reason=error_message,
            run_meta=run_meta,
        )
