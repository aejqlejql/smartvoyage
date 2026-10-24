"""SmartVoyage 内部协议层。

这里定义跨模块共享的业务结果协议。工作流状态由
``core.graph.state.SmartVoyageState`` 负责，专家结果由 ``ExpertResult`` 负责。

这些 contract 不包含具体业务逻辑，作用类似“内部接口文档”，用于约束专家
Agent、A2A 服务和主服务之间的数据边界。
"""

from .expert_result import ExpertResult

__all__ = ["ExpertResult"]
