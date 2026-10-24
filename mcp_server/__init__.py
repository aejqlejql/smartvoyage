"""
MCP（Model Context Protocol）工具服务层。

本包包含三个独立的 MCP 工具服务，每个都是一个独立进程：
    - mcp_weather_server.py → WeatherTools（端口 8002）：天气数据查询
    - mcp_ticket_server.py  → TicketTools（端口 8001）：票务数据查询与预订
    - mcp_trip_server.py    → TripTools（端口 8003）：租车/旅游团/保险

MCP 服务只暴露工具接口（Tool），由上层 A2A 服务通过 LangChain Agent 调用。
每个 MCP 服务通过 FastMCP 框架以 Streamable HTTP 方式对外提供 JSON-RPC 接口。

与 A2A 服务的关键区别：
    - MCP 服务 = 工具层（数据访问，不涉及对话编排）
    - A2A 服务 = 编排层（理解用户意图，选择合适的 MCP 工具执行）
"""
