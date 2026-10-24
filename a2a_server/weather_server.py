"""天气 A2A 服务：通过独立 MCP 服务查询天气数据。"""
# ruff: noqa: E402

import asyncio
import os
from datetime import datetime

# A2A 服务负责接收任务，天气数据和工具执行全部交给远程 MCP 服务。
import pytz
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
# 这些地址都支持环境变量覆盖，便于本地、测试和生产环境切换。
MCP_URL = os.getenv("WEATHER_MCP_URL", "http://127.0.0.1:8002").rstrip("/")
A2A_PORT = int(os.getenv("WEATHER_A2A_PORT", "5005"))
A2A_URL = os.getenv("WEATHER_A2A_URL", f"http://127.0.0.1:{A2A_PORT}")

llm = ChatOpenAI(
    model=conf.model_name,
    base_url=conf.base_url,
    api_key=conf.api_key,
    temperature=conf.temperature,
)
# MultiServerMCPClient 只保存远程 MCP 配置，真正调用 get_tools 时才建立连接。
mcp_client = MultiServerMCPClient({
    "weather": {
        "url": f"{MCP_URL}/mcp",
        "transport": "streamable_http",
    }
})

agent_card = AgentCard(
    name="WeatherQueryAssistant",
    description="通过 MCP 查询天气数据的助手",
    url=A2A_URL,
    version="3.0.0",
    skills=[
        AgentSkill(
            name="query_weather",
            description="查询指定城市和日期范围的天气",
            examples=["查询北京明天天气", "查询上海未来三天的天气"],
        )
    ],
)


def text_artifact(text: str):
    """将普通文本包装成 python_a2a 任务产物格式。"""
    return [{"parts": [{"type": "text", "text": str(text)}]}]


def message_text(message) -> str:
    """兼容 LangChain 文本消息和内容块列表，提取最终可显示文本。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


async def query_weather(conversation: str) -> str:
    """加载远程 MCP 工具并通过 LangChain Agent 执行查询。"""
    # 每次任务按当前 MCP 服务端工具定义创建 Agent，避免本地复制工具协议。
    tools = await mcp_client.get_tools(server_name="weather")
    current_date = datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d")
    system_prompt = f"""你是天气查询助手，只能通过提供的 MCP 工具查询天气。
从用户问题中提取城市、开始日期和结束日期，日期使用 YYYY-MM-DD。
缺少必要信息时直接向用户追问，不要编造天气数据。
如果工具返回 status=no_data，只能说明数据库或 API 没有该城市/日期的天气记录，不能说服务不可用。
如果工具返回 status=error 或 database_unavailable，才能说明天气数据源不可用，并给出错误原因。
无论哪种失败场景，都禁止补写预计温度、湿度、风向、降水概率等数值。
最终只输出 JSON 对象，不要使用 Markdown。字段固定为 status、agent、data、message、source、error_code。
status 只能是 success、no_data、error、input_required；查询成功时 data 必须来自工具返回的 data 原文。
当前日期是 {current_date}。"""
    agent = create_agent(model=llm, tools=tools, system_prompt=system_prompt)
    response = await agent.ainvoke({
        "messages": [{"role": "user", "content": conversation}]
    })
    # Agent 返回完整消息列表，最后一条消息是本次任务对用户的回复。
    return message_text(response["messages"][-1])


class WeatherQueryServer(A2AServer):
    """处理天气查询任务的 A2A 服务。"""

    def __init__(self):
        """把 WeatherQueryAssistant 的 Agent Card 绑定到 A2A 服务实例。

        python_a2a 会通过该 Agent Card 暴露 ``/.well-known/agent.json``，
        上游 ChatService/AgentRegistry 读取卡片后才能知道该服务名称、URL 和技能。
        """
        super().__init__(agent_card=agent_card)

    def handle_task(self, task):
        """同步处理 A2A 任务，并把异步 MCP 调用结果写回任务对象。"""
        # Flow step 7: 专家 Agent 收到 A2A Task，调用 MCP，并把结果包装为 ExpertResult。
        content = (task.message or {}).get("content", {})
        conversation = content.get("text", "") if isinstance(content, dict) else ""
        if not conversation:
            # 缺少问题时要求客户端补充输入，不应标记为失败。
            task.artifacts = text_artifact(
                ExpertResult.input_required(
                    "WeatherQueryAssistant",
                    "请提供城市和日期范围。",
                ).to_json()
            )
            task.status = TaskStatus(state=TaskState.INPUT_REQUIRED)
            return task

        try:
            # python_a2a 的任务处理器是同步入口，因此在这里桥接异步 Agent。
            result = asyncio.run(query_weather(conversation))
            expert_result = ExpertResult.from_text(
                result,
                agent="WeatherQueryAssistant",
                source="mcp",
            )
            task.artifacts = text_artifact(expert_result.to_json())
            task.status = TaskStatus(
                state=TaskState.INPUT_REQUIRED
                if expert_result.status == "input_required"
                else TaskState.COMPLETED
            )
        except Exception as exc:
            logger.exception("天气 MCP 查询失败")
            task.artifacts = text_artifact(
                ExpertResult.error(
                    "WeatherQueryAssistant",
                    f"天气查询失败：{exc}",
                    error_code="agent_execution_failed",
                    source="mcp",
                ).to_json()
            )
            task.status = TaskStatus(state=TaskState.FAILED)
        return task


if __name__ == "__main__":
    # A2A 和 MCP 必须分别启动，便于独立部署、重启和故障定位。
    server = WeatherQueryServer()
    bind_host = os.getenv("WEATHER_A2A_BIND_HOST", "127.0.0.1")
    print(f"[{server.agent_card.name}] 已启动，在 {A2A_URL}")
    print(f"[{server.agent_card.name}] MCP 地址: {MCP_URL}/mcp")
    run_server(server, host=bind_host, port=A2A_PORT)
