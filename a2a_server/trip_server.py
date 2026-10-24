"""行程 A2A 服务：通过独立 MCP 服务处理租车、旅游团和保险。"""
# ruff: noqa: E402

import asyncio
import os
from datetime import datetime

# 行程 A2A 负责理解用户任务，租车、旅游团和保险能力由远程 MCP 提供。
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
# 通过环境变量覆盖地址，支持多个实例并行部署。
MCP_URL = os.getenv("TRIP_MCP_URL", "http://127.0.0.1:8003").rstrip("/")
A2A_PORT = int(os.getenv("TRIP_A2A_PORT", "5007"))
A2A_URL = os.getenv("TRIP_A2A_URL", f"http://127.0.0.1:{A2A_PORT}")

llm = ChatOpenAI(
    model=conf.model_name,
    base_url=conf.base_url,
    api_key=conf.api_key,
    temperature=conf.temperature,
)
mcp_client = MultiServerMCPClient({
    "trip": {
        "url": f"{MCP_URL}/mcp",
        "transport": "streamable_http",
    }
})

agent_card = AgentCard(
    name="TripAssistant",
    description="通过 MCP 提供租车、旅游团和保险服务的行程助手",
    url=A2A_URL,
    version="3.0.0",
    skills=[
        AgentSkill(
            name="trip_service",
            description="查询或预订租车、旅游团和旅行保险",
            examples=["查询北京租车", "购买综合型旅行保险"],
        )
    ],
)


def text_artifact(text: str):
    """将 Agent 回复包装成 python_a2a 的 artifacts 结构。"""
    return [{"parts": [{"type": "text", "text": str(text)}]}]


def message_text(message) -> str:
    """把 LangChain 的消息对象或内容块列表统一转换为字符串。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


async def run_trip_agent(query: str) -> str:
    """通过远程行程 MCP 工具处理一次租车、旅游团或保险任务。"""
    # 运行时获取工具，确保 Agent 与 MCP 服务端实际暴露的能力一致。
    tools = await mcp_client.get_tools(server_name="trip")
    system_prompt = f"""你是行程管家，只能通过提供的 MCP 工具处理租车、旅游团和保险。
先判断用户是在查询还是预订，再提取工具所需参数。
缺少必要参数时向用户追问，不能编造价格、余票或库存。
如果工具返回 status=no_data，只能说明数据库中暂无匹配行程记录。
如果工具返回 status=error 或 database_unavailable，只能说明行程数据源不可用及原因。
无数据或错误时禁止补写租车价格、保险价格、旅游团价格、库存或所谓参考均价。
最终只输出 JSON 对象，不要使用 Markdown。字段固定为 status、agent、data、message、source、error_code。
status 只能是 success、no_data、error、input_required；查询成功时 data 必须来自工具返回的 data 原文。
当前日期是 {datetime.now().strftime('%Y-%m-%d')}。"""
    agent = create_agent(model=llm, tools=tools, system_prompt=system_prompt)
    response = await agent.ainvoke({
        "messages": [{"role": "user", "content": query}]
    })
    # A2A 只需要最终文本，不把工具调用中间消息继续向上游传递。
    return message_text(response["messages"][-1])


class TripServer(A2AServer):
    """处理行程查询和预订任务的 A2A 服务。"""

    def __init__(self):
        """绑定 TripAssistant 的 Agent Card。

        该卡片声明租车、旅游团、保险相关能力，主服务据此把行程类意图路由到本服务。
        """
        super().__init__(agent_card=agent_card)

    def handle_task(self, task):
        """处理 A2A 任务并根据 Agent 结果设置任务状态。"""
        # Flow step 7: 行程专家负责租车、旅游团、保险任务，数据来自 Trip MCP。
        content = (task.message or {}).get("content", {})
        query = content.get("text", "") if isinstance(content, dict) else ""
        if not query:
            # 缺少用户问题时等待补充输入，避免触发无效的 MCP 调用。
            task.artifacts = text_artifact(
                ExpertResult.input_required(
                    "TripAssistant",
                    "请提供行程查询或预订内容。",
                ).to_json()
            )
            task.status = TaskStatus(state=TaskState.INPUT_REQUIRED)
            return task

        try:
            result = asyncio.run(run_trip_agent(query))
            expert_result = ExpertResult.from_text(
                result,
                agent="TripAssistant",
                source="mcp",
            )
            task.artifacts = text_artifact(expert_result.to_json())
            # 识别 Agent 的追问文本，保留 input-required 状态供客户端继续对话。
            state = (
                TaskState.INPUT_REQUIRED
                if expert_result.status == "input_required"
                else TaskState.COMPLETED
            )
            task.status = TaskStatus(state=state)
        except Exception as exc:
            logger.exception("行程 MCP 查询失败")
            task.artifacts = text_artifact(
                ExpertResult.error(
                    "TripAssistant",
                    f"行程处理失败：{exc}",
                    error_code="agent_execution_failed",
                    source="mcp",
                ).to_json()
            )
            task.status = TaskStatus(state=TaskState.FAILED)
        return task


if __name__ == "__main__":
    # A2A 与行程 MCP 分开启动，两个进程之间通过 Streamable HTTP 通信。
    server = TripServer()
    bind_host = os.getenv("TRIP_A2A_BIND_HOST", "127.0.0.1")
    print(f"[{server.agent_card.name}] 已启动，在 {A2A_URL}")
    print(f"[{server.agent_card.name}] MCP 地址: {MCP_URL}/mcp")
    run_server(server, host=bind_host, port=A2A_PORT)
