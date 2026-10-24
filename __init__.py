"""
SmartVoyage —— 基于 A2A（Agent-to-Agent）协议的智能旅行助手系统。

架构概览：
    api/server.py          → FastAPI 入口，提供 REST/SSE 接口和静态页面
    app/chat/service.py    → 对话门面，装配 LLM、记忆、AgentRegistry、LangGraph
    core/graph/            → LangGraph 工作流状态图（direct / ReAct / collaborative）
    core/collaboration_engine.py → Planner/Expert/Critic/Summary 强多智能体协作
    core/trace_recorder.py → 最近一次运行的结构化 trace
    core/graph/state.py    → SmartVoyageState 工作流状态协议
    agents/registry.py     → A2A Agent 注册、Agent Card 和工具列表
    infrastructure/config.py → 全局配置（LLM、数据库、天气 API、Milvus 等）
    app/memory/conversation.py → 对话记忆管理（短期记忆 + 用户偏好 + MySQL 持久化）
    prompts/smartvoyage.py  → 所有 LLM Prompt 模板集中管理
    infrastructure/logging.py → 统一日志记录器工厂

    a2a_server/            → A2A 子代理服务（天气、票务、行程）
    mcp_server/            → MCP 工具服务（数据库查询、API 调用）
    utils/                 → 工具模块（格式化、天气爬虫）
    sql/                   → 数据库脚本（建表、数据初始化、RAG）
    tests/                 → 单元测试与集成测试
    static/                → 前端页面

启动方式：
    1. 启动 MySQL + Milvus（如需 RAG 功能）
    2. 依次启动 MCP 子服务（mcp_ticket_server / mcp_weather_server / mcp_trip_server）
    3. 依次启动 A2A 子服务（ticket_server / weather_server / trip_server）
    4. 启动主服务：python -m ai01_SmartVoyage.api.server
    5. 浏览器访问：http://127.0.0.1:8080
"""
