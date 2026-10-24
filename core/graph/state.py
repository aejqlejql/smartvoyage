"""SmartVoyage LangGraph 状态定义。

所有节点都只读取和返回这份状态的局部更新。学生可以把它理解为一次
用户请求的“工作流上下文”，而不是散落在多个函数参数里的临时变量。
"""

from typing import Any

from typing_extensions import TypedDict


class SmartVoyageState(TypedDict, total=False):
    """一次 SmartVoyage 请求在 LangGraph 中流转的完整状态。"""

    # 用户输入和本次运行的可追踪元信息。
    user_input: str
    run_id: str
    created_at: str

    # 工作流状态和当前选择的执行模式。
    status: str
    mode: str
    route: str
    plan_route: str

    # 意图识别和普通规划结果。
    intents: list[str]
    user_queries: dict[str, Any]
    follow_up_message: str
    plan: dict[str, Any]

    # 执行结果和最终响应。
    execution_trace: list[dict[str, Any]]
    response: str
    collaboration_used: bool

    # 错误收口信息。
    error_message: str
