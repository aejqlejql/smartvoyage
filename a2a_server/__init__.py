"""
A2A（Agent-to-Agent）服务层。

本包包含三个独立的 A2A 子代理服务，每个服务都是一个独立进程：
    - weather_server.py  → WeatherQueryAssistant（端口 5005）：天气查询
    - ticket_server.py   → TicketAssistant（端口 5006）：票务查询与预订
    - trip_server.py     → TripAssistant（端口 5007）：租车/旅游团/保险

A2A 服务负责对话编排（通过 LangChain Agent 调用 MCP 工具），
MCP 服务负责数据访问（数据库查询、API 调用），两者分离部署。
"""
