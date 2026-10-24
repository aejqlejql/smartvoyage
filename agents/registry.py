"""SmartVoyage A2A Agent 注册表。

AgentRegistry 是主服务和独立 A2A 专家服务之间的边界层。它负责：

1. 从环境变量读取各专家 Agent 的 URL。
2. 创建 python-a2a 的 A2AClient，并注册到 AgentNetwork。
3. 对 ChatService/LangGraph 提供统一的 get_agent、get_agent_cards、
   get_available_tools 能力。

业务查询不在这里执行；这里仅管理“有哪些 Agent、它们在哪里、它们声明了什么能力”。
"""

import os

from python_a2a import AgentNetwork
from python_a2a.client import A2AClient


class AgentRegistry:
    """注册 A2A 专家 Agent，并向上层暴露 Agent Card。"""

    def __init__(self):
        """读取 Agent URL 配置，创建 AgentNetwork，并注册全部专家 Agent。"""
        # Flow step 6: A2A 路由表。ChatService 只认 Agent 名称，不关心服务实际部署在哪。
        # 每个 URL 都允许通过环境变量覆盖，便于本地端口调整或服务拆到远程机器。
        self.agent_urls = {
            "WeatherQueryAssistant": os.getenv(
                "WEATHER_A2A_URL", "http://127.0.0.1:5005"
            ),
            "TicketAssistant": os.getenv(
                "TICKET_A2A_URL", "http://127.0.0.1:5006"
            ),
            "TripAssistant": os.getenv(
                "TRIP_A2A_URL", "http://127.0.0.1:5007"
            ),
        }
        self.agent_network = AgentNetwork(name="旅行助手网络")
        for agent_name, agent_url in self.agent_urls.items():
            # A2AClient 是协议客户端，timeout 给长链路 LLM + MCP 调用留出足够时间。
            self.agent_network.add(
                agent_name,
                A2AClient(agent_url, timeout=120),
            )

    def get_agent(self, agent_name: str):
        """按名称获取已注册的 A2A Agent 客户端。"""
        return self.agent_network.get_agent(agent_name)

    def get_agent_cards(self) -> list:
        """读取所有 Agent Card，并整理成前端和 prompt 都容易消费的结构。"""
        cards = []
        for agent_name in self.agent_network.agents.keys():
            agent_card = self.agent_network.get_agent_card(agent_name)
            cards.append({
                "name": agent_name,
                "skills": [
                    f"{skill.name}: {skill.description}"
                    for skill in agent_card.skills
                ],
                "description": agent_card.description,
                "url": self.agent_urls.get(agent_name, "未知地址"),
                "status": "在线",
            })
        return cards

    def get_available_tools(self) -> str:
        """把 Agent Card 转换成 planning prompt 可读的工具列表文本。"""
        lines = []
        for card in self.get_agent_cards():
            lines.append(f"- Agent: {card['name']}, {card['description']}")
            for skill in card["skills"]:
                lines.append(f"  Skill: {skill}")
        return "\n".join(lines)
