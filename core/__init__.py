"""SmartVoyage 核心编排层。

本包放置主对话服务背后的“流程控制组件”，不直接暴露 HTTP 接口，也不直接
实现业务工具查询。它的职责是把一次用户请求组织成可追踪的工作流：

- ``graph/``：LangGraph 主工作流，决定 direct、ReAct 或 collaborative 路径。
- ``collaboration_engine.py``：复杂请求的 Planner/Expert/Critic/Summary 协作链。
- ``trace_recorder.py``：记录最近一次运行的规划、执行、评审和汇总轨迹。
- ``message_utils.py``：LLM 输出解析和查询对象标准化等通用小工具。

拆出这一层的目的，是让 ``app/chat/service.py`` 保持为入口门面，避免继续膨胀成
“所有逻辑都在一个类里”的中心大文件。
"""
