"""票务 A2A 服务：通过独立 MCP 服务查询和预订票务。"""
# ruff: noqa: E402

import asyncio
import os
from datetime import datetime

# 票务 A2A 只负责对话编排，数据库查询和预订工具位于独立 MCP 服务。
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from python_a2a import (
    A2AServer,
    AgentCard,
    AgentSkill,
    TaskState,
    TaskStatus,
    run_server,
)

from ..contracts.expert_result import ExpertResult
from ..infrastructure.config import Config
from ..infrastructure.logging import logger

conf = Config()
# 地址和端口均由环境变量控制，避免把部署拓扑写死在业务代码中。
MCP_URL = os.getenv("TICKET_MCP_URL", "http://127.0.0.1:8001").rstrip("/")
A2A_PORT = int(os.getenv("TICKET_A2A_PORT", "5006"))
A2A_URL = os.getenv("TICKET_A2A_URL", f"http://127.0.0.1:{A2A_PORT}")

llm = ChatOpenAI(
    model=conf.model_name,
    base_url=conf.base_url,
    api_key=conf.api_key,
    temperature=conf.temperature,
)
mcp_client = MultiServerMCPClient({
    "ticket": {
        "url": f"{MCP_URL}/mcp",
        "transport": "streamable_http",
    }
})

agent_card = AgentCard(
    name="TicketAssistant",
    description="通过 MCP 提供票务查询和预订服务的助手",
    url=A2A_URL,
    version="3.0.0",
    skills=[
        AgentSkill(
            name="ticket_service",
            description="查询或预订火车票、机票和演出票",
            examples=["查询北京到上海的火车票", "预订一张演唱会门票"],
        )
    ],
)


def text_artifact(text: str):
    """将文本转换为 A2A artifacts 结构。"""
    return [{"parts": [{"type": "text", "text": str(text)}]}]


def message_text(message) -> str:
    """提取 LangChain 消息中的文本，兼容字符串和多内容块响应。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


async def run_ticket_agent(query: str) -> str:
    """通过远程票务 MCP 工具执行一次查询或预订任务。"""
    # 工具清单来自 MCP 服务端，Agent 不直接依赖数据库实现。
    tools = await mcp_client.get_tools(server_name="ticket")
    system_prompt = f"""你是票务助手，只能通过提供的 MCP 工具处理火车票、机票和演出票。
先判断用户是在查询还是预订，再提取工具所需参数。
缺少必要参数时向用户追问，不能编造车次、航班或余票。
如果工具返回 status=no_data，只能说明数据库中暂无匹配票务记录。
如果工具返回 status=error 或 database_unavailable，只能说明票务数据源不可用及原因。
无数据或错误时禁止补写价格、余票、时间、车次、航班号或所谓参考均价。
最终只输出 JSON 对象，不要使用 Markdown。字段固定为 status、agent、data、message、source、error_code。
status 只能是 success、no_data、error、input_required；查询成功时 data 必须来自工具返回的 data 原文。
当前日期是 {datetime.now().strftime('%Y-%m-%d')}。"""
    agent = create_agent(model=llm, tools=tools, system_prompt=system_prompt)
    response = await agent.ainvoke({
        "messages": [{"role": "user", "content": query}]
    })
    # 仅把最终消息返回给 A2A 客户端，避免暴露 Agent 的中间过程。
    return message_text(response["messages"][-1])


class TicketServer(A2AServer):
    """处理票务查询和预订任务的 A2A 服务。"""

    def __init__(self):
        """绑定 TicketAssistant 的 Agent Card。

        Agent Card 是 A2A 协议的能力声明，上游主服务通过它识别该 Agent
        能处理机票、火车票、演唱会票查询与预订。
        """
        super().__init__(agent_card=agent_card)

    def handle_task(self, task):
        """将 A2A 任务转换为 Agent 查询，并设置标准任务状态。"""
        # Flow step 7: 票务专家只负责理解票务任务，真实数据查询交给 Ticket MCP。
        content = (task.message or {}).get("content", {})
        query = content.get("text", "") if isinstance(content, dict) else ""
        if not query:
            # 参数不完整是可恢复状态，客户端补充内容后可以继续执行。
            task.artifacts = text_artifact(
                ExpertResult.input_required(
                    "TicketAssistant",
                    "请提供票务查询或预订内容。",
                ).to_json()
            )
            task.status = TaskStatus(state=TaskState.INPUT_REQUIRED)
            return task

        try:
            result = asyncio.run(run_ticket_agent(query))
            expert_result = ExpertResult.from_text(
                result,
                agent="TicketAssistant",
                source="mcp",
            )
            task.artifacts = text_artifact(expert_result.to_json())
            # Agent 明确追问参数时返回 input-required，而不是误报 completed。
            state = (
                TaskState.INPUT_REQUIRED
                if expert_result.status == "input_required"
                else TaskState.COMPLETED
            )
            task.status = TaskStatus(state=state)
        except Exception as exc:
            logger.exception("票务 MCP 查询失败")
            task.artifacts = text_artifact(
                ExpertResult.error(
                    "TicketAssistant",
                    f"票务处理失败：{exc}",
                    error_code="agent_execution_failed",
                    source="mcp",
                ).to_json()
            )
            task.status = TaskStatus(state=TaskState.FAILED)
        return task


if __name__ == "__main__":
    # 该脚本只启动票务 A2A 进程，票务 MCP 需要另开进程启动。
    server = TicketServer()
    bind_host = os.getenv("TICKET_A2A_BIND_HOST", "127.0.0.1")
    print(f"[{server.agent_card.name}] 已启动，在 {A2A_URL}")
    print(f"[{server.agent_card.name}] MCP 地址: {MCP_URL}/mcp")
    run_server(server, host=bind_host, port=A2A_PORT)
