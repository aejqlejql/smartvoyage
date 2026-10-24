"""A2A Agent 访问层。

本包负责把外部独立运行的专家 Agent 注册进主服务可调用的网络中。
当前主要由 ``registry.py`` 提供 ``AgentRegistry``：

- 读取 A2A 服务地址和端口配置。
- 创建 A2AClient 并注册天气、票务、行程三个专家 Agent。
- 对上层提供 Agent Card 和工具列表，供规划 prompt 和前端展示使用。

这里不处理业务查询结果，也不做 LLM 总结；这些仍由 ChatService/LangGraph
调用链完成。
"""
