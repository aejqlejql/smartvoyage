"""
需求：SmartVoyage核心对话服务，供CLI和API共同调用

架构说明：
    本模块是主对话服务门面，负责装配 LLM、记忆、AgentRegistry、
    CollaborationEngine、TraceRecorder 和 SmartVoyageWorkflowGraph。

    真正的一次用户请求状态机由 SmartVoyageWorkflowGraph 执行，本模块保留以下能力供其调用：
    1. 意图识别（intent_agent）：分析用户输入，识别出用户想做什么（查天气/查机票/查景点等）
    2. 启发式路由（_should_skip_planning）：快速判断任务复杂度
       - 单意图或独立多意图：跳过规划，直接执行（省去 1 次 LLM 调用）
       - 含依赖关系的复杂任务：调用 planning_agent 生成执行计划
    3. ReAct循环（react_loop）：针对复杂任务，逐步执行每个步骤
       - Action（行动）：直接调用对应的 agent 执行查询（跳过 Thought 推理，省去每步 1 次 LLM 调用）
       - Observation（观察）：记录执行结果，供后续步骤参考
    4. 结果汇总：所有步骤完成后，生成一条连贯的最终回复

整体流程：
    API → ChatService.chat/chat_stream → LangGraph → direct / ReAct / collaborative → 返回回复

A2A架构说明：
    Agent2Agent (A2A) 是一种代理间通信协议。本系统通过 AgentRegistry 管理多个子代理：
    - WeatherQueryAssistant（天气查询代理）：运行在 127.0.0.1:5005
    - TicketAssistant（票务代理）：运行在 127.0.0.1:5006
    - TripAssistant（行程管家代理）：运行在 127.0.0.1:5007
    主助手通过 A2A 协议向子代理发送任务请求，获取查询结果后整合回复用户。

架构图：

+─────────────────────────────────────────────────────────────────────────────+
│                           ChatService  类                                   │
│                                                                             │
│  ┌────────────────── 成员变量 ──────────────────┐                           │
│  │  agent_registry        ── A2A Agent 注册表    │                           │
│  │  workflow_graph        ── LangGraph 工作流     │                           │
│  │  collaboration_engine  ── 强多智能体协作引擎   │                           │
│  │  trace_recorder        ── 执行轨迹记录器       │                           │
│  │  llm                   ── ChatOpenAI (LLM)    │                           │
│  │  memory                ── ConversationMemory  │                           │
│  │  messages[]            ── 当前对话消息列表     │                           │
│  │  conversation_history  ── 完整对话字符串       │                           │
│  └───────────────────────────────────────────────┘                           │
│                                                                             │
│  ┌────────────────── 方法分层 ──────────────────┐                           │
│  │                                               │                           │
│  │  [入口层]                                     │                           │
│  │  chat()               ── 标准对话入口          │                           │
│  │  chat_stream()        ── 流式对话入口          │                           │
│  │                                               │                           │
│  │  [智能决策层]                                 │                           │
│  │  intent_agent()       ── 意图识别 (LLM)       │                           │
│  │  _should_skip_planning() ── 启发式路由规则     │                           │
│  │  planning_agent()     ── 任务规划 (LLM)       │                           │
│  │                                               │                           │
│  │  [执行层]                                     │                           │
│  │  react_loop()         ── ReAct 循环 (标准)    │                           │
│  │  execute_step()       ── 执行单个计划步骤      │                           │
│  │                                               │                           │
│  │  [代理调用层]                                 │                           │
│  │  _call_agent_intent()       ── 统一调用 (标准) │                           │
│  │                                               │                           │
│  │  [辅助层]                                     │                           │
│  │  get_available_tools()  ── 动态工具列表        │                           │
│  │  get_agent_cards()      ── 获取代理卡片        │                           │
│  │  _init_db_and_load_memory() ── DB初始化+加载   │                           │
│  │  update_user_profile()  ── 更新用户偏好        │                           │
│  │  get_memory_state()     ── 记忆状态摘要        │                           │
│  │  clear_memory()         ── 清空所有记忆        │                           │
│  └───────────────────────────────────────────────┘                           │
└─────────────────────────────────────────────────────────────────────────────+

请求处理流程 (chat / chat_stream):

    用户输入
       │
       ▼
    ┌─────────────────┐
    │ 1. 写入记忆       │  memory.add_message("user", input)
    └────────┬────────┘
             ▼
    ┌─────────────────┐
    │ 2. 意图识别       │  intent_agent() → LLM → (intents, queries, follow_up)
    └────────┬────────┘
             ▼
    ┌──────────────────────────────────────────────┐
    │ 3. 特殊情况判断                                │
    │    out_of_scope?  → 返回追问                   │
    │    follow_up?     → 返回追问                   │
    └────────────────────┬─────────────────────────┘
                         ▼
    ┌──────────────────────────────────────────────┐
    │ 4. 任务规划                                    │
    │    _should_skip_planning()?                   │
    │      ├─ YES → plan = {need_plan: false}       │
    │      └─ NO  → planning_agent() → LLM 生成计划 │
    └────────────────────┬─────────────────────────┘
                         ▼
    ┌──────────────────────────────────────────────┐
    │ 5. 执行                                        │
    │    need_plan?                                 │
    │      ├─ YES → react_loop()                    │
    │      │         ├─ 按 depends_on 分组           │
    │      │         ├─ 组内并行 execute_step()      │
    │      │         │    → _call_agent_intent()    │
    │      │         └─ 汇总 → LLM 生成连贯回复      │
    │      │                                        │
    │      └─ NO  → 遍历 intents                    │
    │                → _call_agent_intent() 每个意图 │
    │                → 拼接所有回复                  │
    └────────────────────┬─────────────────────────┘
                         ▼
    ┌─────────────────┐
    │ 6. 写入记忆       │  memory.add_message("assistant", response)
    └────────┬────────┘
             ▼
         返回回复

_call_agent_intent() 内部路由:

    intent
       │
       ├─ attraction → attraction_prompt() + LLM → 直接生成
       │
       ├─ flight/train/concert/car_rental/...
       │     │
       │     ├─ 提取实体 → memory.extract_entities()
       │     ├─ 更新上下文 → memory.update_task_context()
       │     ├─ A2A → agent.send_task_async(task)
       │     │        ├─ WeatherQueryAssistant → summarize_weather_prompt() + LLM
       │     │        ├─ TicketAssistant/TicketQueryAssistant/TripAssistant
       │     │        │                          → summarize_ticket_prompt() + LLM
       │     │        └─ 其他 → 直接返回原始结果
       │     └─ 返回总结结果
       │
       └─ 未识别 → "暂不支持此意图"

子代理调用关系:

    ChatService (主助手 :5004)
       │
       ├── [A2A :5005] → WeatherQueryAssistant  (天气查询)
       ├── [A2A :5006] → TicketAssistant        (票务预定)
       ├── [A2A :5007] → TripAssistant          (行程管家: 租车/旅游团/保险)
       │
       └── [直连 LLM]  → attraction (景点推荐，不走子代理)

优化策略总结:

    1. 启发式路由   → 简单任务跳过 planning_agent，省 1 次 LLM 调用
    2. 省略 Thought → ReAct 循环中跳过 Thought 推理，每步省 1 次 LLM 调用
    3. 并行执行     → 无依赖步骤通过 asyncio.gather() 并发执行
    4. 工具缓存     → _available_tools_text 缓存，避免重复查询 agent card
"""

# ==================== 导入依赖 ====================
import asyncio  # 异步IO库，用于并发调用多个 agent（异步网络请求）
import json  # JSON处理，用于解析大模型的JSON输出和序列化数据
import uuid  # 唯一标识符生成，用于给每个任务分配唯一ID

import mysql.connector  # MySQL 数据库驱动，用于记忆持久化
from langchain_core.prompts import ChatPromptTemplate  # LangChain 的提示模板类

# TextContent: 消息文本内容包装
# Message: A2A 消息对象，包含角色和内容
# MessageRole: 消息角色枚举（USER/ASSISTANT）
# Task: A2A 任务对象，用于向子代理发送请求
from langchain_openai import ChatOpenAI  # LangChain 的大模型接口，兼容 OpenAI API 格式

# python_a2a 是 A2A 协议的 Python 客户端库
from python_a2a import Message, MessageRole, Task, TextContent

# 导入项目内部模块
from ...agents.registry import AgentRegistry
from ...contracts.expert_result import ExpertResult
from ...core.collaboration_engine import CollaborationEngine
from ...core.graph.workflow import SmartVoyageWorkflowGraph
from ...core.guardrails import (
    contains_data_source_error,
    contains_explicit_no_data_signal,
    contains_no_data_signal,
    current_date_str,
    current_year,
    normalize_query_payload,
)
from ...core.message_utils import parse_llm_json, query_to_text
from ...core.trace_recorder import TraceRecorder
from ...infrastructure.config import Config
from ...infrastructure.logging import logger
from ...prompts.smartvoyage import SmartVoyagePrompts
from ..memory.conversation import ConversationMemory

conf = Config()  # 全局配置实例，包含模型参数、意图映射等


class ChatService:
    """
    SmartVoyage 对话服务类

    这是整个系统的核心类，负责处理用户的所有输入并生成回复。
    内部流程为：意图识别 → 任务规划 → 执行（直接/ReAct循环）→ 返回回复

    使用示例：
        service = ChatService()          # 创建服务实例
        response = await service.chat("北京明天天气怎么样？")  # 发送用户输入
        print(response)                  # 打印助手回复
    """

    def __init__(self, *, persistent_memory=True):
        """
        初始化 ChatService 实例

        完成以下初始化工作：
        1. 配置子代理网络（注册天气、票务、行程代理）
        2. 初始化大模型连接
        3. 初始化记忆管理器
        4. 装配 TraceRecorder、CollaborationEngine 和 LangGraph 工作流
        5. 初始化数据库连接并加载持久化记忆
        """
        # ========== 1. 配置 A2A 代理网络 ==========
        # AgentRegistry 是“主服务到 A2A 专家服务”的唯一注册入口。
        # ChatService 后续不直接维护 agent_url 或 AgentNetwork，降低新增 Agent 时的改动面。
        self.agent_registry = AgentRegistry()

        # ========== 2. 初始化大模型连接 ==========
        # 使用 LangChain 的 ChatOpenAI 接口连接大模型
        # SiliconFlow 兼容 OpenAI API 格式，所以可以用 ChatOpenAI
        self.llm = ChatOpenAI(
            model=conf.model_name,     # 模型名称，如 "Qwen/Qwen2.5-72B-Instruct"
            api_key=conf.api_key,      # API 密钥
            base_url=conf.base_url,    # API 基础地址
            temperature=0.1,
            timeout=25,
            max_retries=1,
        )

        # ========== 3. 初始化记忆和对话状态 ==========
        self.memory = ConversationMemory(short_term_limit=10)  # 短期记忆最多保留10条对话
        self.messages = []           # 对话消息列表，格式：[{"role": "user/assistant", "content": "..."}]
        self.conversation_history = ""  # 完整对话历史字符串，用于调试和展示

        # TraceRecorder 先创建，因为普通工作流和协作工作流都要把最终 trace 写入这里。
        self.trace_recorder = TraceRecorder()

        # CollaborationEngine 专门处理“综合旅行方案”场景。它复用 _call_agent_intent，
        # 因此不会复制 A2A 调用逻辑；Planner/Critic/Summary 的协作过程集中在该对象中。
        self.collaboration_engine = CollaborationEngine(
            llm=self.llm,
            memory=self.memory,
            call_agent_intent=self._call_agent_intent,
            trace_recorder=self.trace_recorder,
            intent_map=conf.intent,
        )

        # LangGraph 只负责状态流转；节点内部复用本服务已有的意图识别、
        # 普通规划、Agent 调用、协作引擎和记忆写入能力。
        self.workflow_graph = SmartVoyageWorkflowGraph(
            intent_agent=self.intent_agent,
            should_use_collaborative_planning=self._should_use_collaborative_planning,
            collaborative_plan=self.collaborative_plan,
            should_skip_planning=self._should_skip_planning,
            planning_agent=self.planning_agent,
            react_loop=self.react_loop,
            call_agent_intent=self._call_agent_intent,
            record_user_message=self._record_user_message,
            record_assistant_message=self._record_assistant_message,
            record_error_message=self._record_error_message,
            trace_recorder=self.trace_recorder,
            intent_map=conf.intent,
        )

        # 城市提取 prompt，用于从景点推荐查询中提取目的地城市
        self._city_extract_prompt = ChatPromptTemplate.from_template(
"""从以下用户查询和对话历史中提取目的地城市（用于天气查询）。规则：
- 只输出城市名称，不要输出其他内容。
- 如果查询中没有明确城市，尝试从对话历史中推断。
- 如果无法确定城市，输出"未知"。

对话历史：{conversation_history}
用户查询：{query}
""")

        # ========== 4. 初始化数据库连接，加载持久化记忆 ==========
        if persistent_memory:
            self._init_db_and_load_memory()

    @staticmethod
    def _query_to_text(query) -> str:
        """把 LLM 产出的查询对象统一转换成可传给 Agent 的文本。"""
        return query_to_text(query)

    @staticmethod
    def _parse_llm_json(text: str) -> dict:
        """解析 LLM JSON 输出，兼容代码块和少量前后缀文本。"""
        return parse_llm_json(text)

    @staticmethod
    def _normalize_text_dates(value):
        """把查询中的无年份日期补成绝对日期。"""
        return normalize_query_payload(value, default_year=current_year())

    @staticmethod
    def _today() -> str:
        """返回当前北京时间日期。"""
        return current_date_str()

    @staticmethod
    def _is_no_data_result(text: str) -> bool:
        """判断 Agent 返回是否表示无数据或不可用。"""
        expert_result = ExpertResult.from_text(text)
        return expert_result.unavailable or contains_no_data_signal(text)

    @staticmethod
    def _parse_expert_result(agent_name: str, raw_response: str) -> ExpertResult:
        """把 A2A artifact 文本收敛成统一专家结果。"""
        return ExpertResult.from_text(
            raw_response,
            agent=agent_name,
            source="a2a",
        )

    @staticmethod
    def _fallback_service_reply(agent_name: str, raw_response: str = "") -> str:
        """区分数据源故障和查询无记录，避免 LLM 补数值。"""
        expert_result = ExpertResult.from_text(raw_response, agent=agent_name)
        if expert_result.status == "input_required" and expert_result.message:
            return expert_result.message

        if contains_explicit_no_data_signal(raw_response):
            if agent_name == "WeatherQueryAssistant":
                return "数据库中暂无该日期天气记录，请确认城市、日期或补充年份。"
            if agent_name in ("TicketQueryAssistant", "TicketAssistant"):
                return "数据库中暂无匹配的票务记录，请确认出发地、到达地、日期或预订条件。"
            if agent_name == "TripAssistant":
                return "数据库中暂无匹配的行程记录，请确认城市、日期或服务类型。"
            return "暂无匹配数据。"

        if contains_data_source_error(raw_response):
            if agent_name == "WeatherQueryAssistant":
                return "天气数据源暂时不可用，原因是数据库或网络连接失败，请稍后重试。"
            if agent_name in ("TicketQueryAssistant", "TicketAssistant"):
                return "票务数据源暂时不可用，原因是数据库或网络连接失败，请稍后重试。"
            if agent_name == "TripAssistant":
                return "行程数据源暂时不可用，原因是数据库或网络连接失败，请稍后重试。"
            return "数据源暂时不可用，请稍后重试。"

        if agent_name == "WeatherQueryAssistant":
            return "暂无该日期天气数据，请确认城市、日期或补充年份。"
        if agent_name in ("TicketQueryAssistant", "TicketAssistant"):
            return "暂无票务数据，请确认出发地、到达地、日期或预订条件。"
        if agent_name == "TripAssistant":
            return "暂无行程数据，请确认城市、日期或服务类型。"
        return "暂无可用数据。"

    def get_collaboration_trace(self) -> dict:
        """返回最近一次对话的规划、执行、评审与汇总轨迹。"""
        return self.trace_recorder.get()

    def _record_user_message(self, user_input: str):
        """写入用户消息，供 LangGraph 当前运行读取上下文。"""
        self.memory.add_message("user", user_input)
        self.messages.append({"role": "user", "content": user_input})
        self.conversation_history += f"\nUser: {user_input}"

    def _record_assistant_message(self, response: str):
        """写入助手回复，保持短期记忆、展示历史和运行状态一致。"""
        self.memory.add_message("assistant", response)
        self.conversation_history += f"\nAssistant: {response}"
        self.messages.append({"role": "assistant", "content": response})

    def _record_error_message(self, error_message: str):
        """写入错误回复，保持与原聊天流程一致。"""
        self.memory.add_message("assistant", error_message)
        self.messages.append({"role": "assistant", "content": error_message})

    def get_available_tools(self) -> str:
        """
        从 A2A 代理网络中动态获取可用工具列表，格式化为文本

        这是 ReAct 架构的关键：告诉大模型当前有哪些工具/agent 可以调用，
        让大模型在推理时从这些工具中选择合适的。

        优势：当新增或删除 agent 时，无需修改 prompt 模板，系统自动感知变化。

        返回值：
            str: 格式化的工具列表文本，例如：
                - Agent: WeatherQueryAssistant, 查询天气信息
                  Skill: 天气查询: 根据城市查询天气
                - Agent: TicketQueryAssistant, 查询票务信息
                  Skill: 票务查询: 查询机票、火车票等
        """
        return self.agent_registry.get_available_tools()

    def _init_db_and_load_memory(self):
        """
        初始化数据库连接，并从数据库加载持久化记忆

        启动流程：
        1. 创建 MySQL 连接，注入到 memory 实例
        2. 从数据库加载用户偏好、查询历史、短期对话
        """
        db_conn = mysql.connector.connect(
            host=conf.host,
            user=conf.user,
            password=conf.password,
            database=conf.database,
            connection_timeout=conf.db_connect_timeout,
        )
        self.memory.set_db_connection(db_conn)
        logger.info("数据库连接初始化成功")

        # 从数据库恢复记忆数据
        self.memory.load_profile_from_db()
        self.memory.load_entities_from_db()
        self.memory.load_messages_from_db()

        # 将加载的短期消息同步到 messages 列表
        for msg in self.memory.short_term_messages:
            self.messages.append({"role": msg["role"], "content": msg["content"]})

        logger.info(f"记忆加载完成: 偏好={len(self.memory.user_profile)}项, "
                    f"历史={len(self.memory.entity_history)}条, 对话={len(self.memory.short_term_messages)}条")

    async def _fetch_weather_for_attraction(self, query_str: str) -> str:
        """
        为景点推荐查询目的地天气，返回简要天气信息。
        如果无法确定目的地，返回空字符串。

        参数：
            query_str (str): 用户的景点查询内容

        返回值：
            str: 天气信息摘要，或空字符串
        """
        try:
            # 从查询中提取目的地城市
            chain = self._city_extract_prompt | self.llm
            city = chain.invoke({
                "conversation_history": self.memory.get_short_term_text(),
                "query": query_str
            }).content.strip()

            if city == "未知" or not city:
                logger.info(f"无法从查询中提取目的地城市: {query_str}")
                return ""

            logger.info(f"从景点查询中提取到城市: {city}")

            # 通过天气代理查询该城市天气
            agent = self.agent_registry.get_agent("WeatherQueryAssistant")
            if agent is None:
                return ""

            weather_query = f"{city}明天天气"
            msg = Message(content=TextContent(text=weather_query), role=MessageRole.USER)
            task = Task(id="task-" + str(uuid.uuid4()), message=msg.to_dict())

            raw_response = await asyncio.get_event_loop().run_in_executor(
                None, lambda: asyncio.run(agent.send_task_async(task))
            )

            if raw_response.status.state == 'completed' and raw_response.artifacts:
                return raw_response.artifacts[0]['parts'][0]['text']
            return ""

        except Exception as e:
            logger.warning(f"查询景点天气失败: {e}")
            return ""

    def update_user_profile(self, profile: dict):
        """
        更新用户偏好设置，由前端 API 直接调用

        参数：
            profile (dict): 用户偏好键值对，如 {"seat_type": "二等座", "cabin_type": "经济舱"}
                           会合并到现有偏好中（新增/覆盖），不会删除已有项
        """
        self.memory.update_profile(profile)
        logger.info(f"更新用户偏好: {profile}")

    def intent_agent(self, user_input: str):
        """
        意图识别：分析用户输入，判断用户想做什么

        工作流程：
        1. 将用户输入 + 对话历史 + 装用户偏好等上下文组后发送给大模型
        2. 大模型返回 JSON，包含识别到的意图列表、改写后的查询、追问消息

        支持的意图类型：
            - weather: 天气查询
            - flight: 机票查询
            - train: 高铁/火车票查询
            - concert: 演唱会票查询
            - order: 票务预定
            - attraction: 景点推荐
            - out_of_scope: 超出系统能力范围

        参数：
            user_input (str): 用户本次的输入内容

        返回值：
            tuple: (intents, user_queries, follow_up_message)
                - intents (list): 识别到的意图列表，如 ["weather", "flight"]
                - user_queries (dict): 改写后的查询，如 {"weather": "北京明天天气", "flight": "北京到上海机票"}
                - follow_up_message (str): 追问消息，当意图不明确时用于追问用户
        """
        # Flow step 4: 意图识别。这里只做“理解和改写”，不直接调用 MCP 或数据库。
        # 组装 Prompt 模板 + 大模型，形成完整的处理链
        chain = SmartVoyagePrompts.intent_prompt() | self.llm

        # 获取当前日期，注入到 prompt 中（用户可能说"明天"、"后天"等相对时间）
        current_date = self._today()
        normalized_query = self._normalize_text_dates(user_input)

        # 调用大模型，传入所有上下文信息
        intent_response = chain.invoke({
            "conversation_history": self.memory.get_short_term_text(),  # 短期对话历史
            "query": normalized_query,    # 用户本次输入（补全无年份日期）
            "current_date": current_date, # 当前日期
            "user_profile": self.memory.get_profile_text(),  # 用户偏好（如"二等座"、"经济舱"）
            "task_context": json.dumps(self.memory.current_task, ensure_ascii=False)  # 当前任务上下文
        }).content.strip()  # .content 获取文本内容，.strip() 去掉首尾空白
        logger.info(f"意图识别原始响应: {intent_response}")

        # 大模型有时会返回 ```json ... ``` 格式的代码块，需要用正则去掉
        # 将 JSON 字符串解析为 Python 字典
        intent_output = self._parse_llm_json(intent_response)
        logger.info(f"解析后响应: {intent_output}")
        intents = intent_output.get("intents", [])  # 意图列表，如 ["weather", "flight"]
        user_queries = self._normalize_text_dates(
            intent_output.get("user_queries", {})
        )  # 改写后的查询字典
        follow_up_message = intent_output.get("follow_up_message", "")  # 追问消息
        logger.info(f"intents: {intents}||user_queries: {user_queries}||follow_up_message: {follow_up_message} ")

        return intents, user_queries, follow_up_message

    def _should_skip_planning(self, intents: list) -> bool:
        """
        启发式判断：是否可以跳过 planning_agent，直接执行

        判断规则（满足任一即跳过）：
        1. 单意图：只有一个意图，无需拆解
        2. 多意图但全部为独立查询类意图（weather/flight/train/concert/attraction），
           彼此之间无依赖，可以直接并行或串行执行

        需要进入 planning 的情况：
        - 包含 order（预订）等需要分步决策的意图
        - 意图之间存在明显的先后依赖（如先查天气再决定是否租车）

        优化收益：省去 1 次 LLM 调用（planning_agent），大幅降低简单请求的响应时间

        参数：
            intents (list): 识别到的意图列表

        返回值：
            bool: True 表示跳过 planning_agent，False 表示需要调用 planning_agent
        """
        # 规则1：单意图直接跳过
        if len(intents) <= 1:
            return True

        # 规则2：多意图但全部为独立查询类，无需分步规划
        independent_intents = {"weather", "flight", "train", "concert", "attraction",
                               "car_rental", "tour_group", "insurance", "trip_order"}
        for intent in intents:
            if intent not in independent_intents:
                # 存在需要分步的意图（如 order），不跳过
                return False
        return True

    def planning_agent(self, intents: list, user_queries: dict) -> dict:
        """
        任务规划：直接进行任务的拆解

        注意：在调用此方法前会先通过 _should_skip_planning 做启发式判断，
        只有复杂任务才会进入此方法，省去不必要的 LLM 调用。

        参数：
            intents (list): 识别到的意图列表，如 ["weather", "flight", "attraction"]
            user_queries (dict): 改写后的查询字典，如 {"weather": "...", "flight": "..."}

        返回值：
            dict: 规划结果，格式为：
                - 简单任务：{"need_plan": false, "reason": "单意图，直接查询即可", "steps": []}
                - 复杂任务：{"need_plan": true, "reason": "多意图需要分步执行",
                            "steps": [{"step": 1, "action": "...", "intent": "weather", "depends_on": 0}, ...]}
        """
        chain = SmartVoyagePrompts.planning_prompt() | self.llm
        current_date = self._today()

        planning_response = chain.invoke({
            "conversation_history": self.memory.get_short_term_text(),
            "query": self._normalize_text_dates(
                self.messages[-1]["content"] if self.messages else ""
            ),
            "current_date": current_date,
            "intents": json.dumps(intents, ensure_ascii=False),
            "user_queries": json.dumps(
                self._normalize_text_dates(user_queries), ensure_ascii=False
            )
        }).content.strip()
        logger.info(f"规划响应: {planning_response}")

        plan = self._parse_llm_json(planning_response)
        return plan

    async def _call_agent_intent(self, intent: str, query_str: str) -> str:
        """
        统一调用 agent 的底层逻辑

        这是一个私有方法（以 _ 开头），不直接对外暴露，供以下两个场景复用：
        1. 简单任务直接执行时（chat 方法的 else 分支）
        2. ReAct 循环中的步骤执行时（execute_step 方法）

        这样做的好处：避免了代码重复，修改 agent 调用逻辑时只需改一处。

        处理逻辑说明：
        - 如果是景点推荐（attraction），直接让大模型生成，不需要调子代理
        - 如果是其他意图，通过 A2A 协议调用对应的子代理
        - 收到结果后，根据代理类型做适当的总结（天气/票务需要总结，其他直接返回）

        参数：
            intent (str): 意图类型，如 "weather"、"flight"、"attraction"
            query_str (str): 查询内容，如 "北京明天天气"

        返回值：
            str: agent 返回的结果文本
        """
        # Flow step 6-10: 从 intent 找到 A2A Agent，拿到 ExpertResult 后再决定是否总结。
        query_str = self._query_to_text(query_str)
        query_str = self._normalize_text_dates(query_str)
        # 根据意图类型，从配置中查找对应的 agent 名称
        agent_name = conf.intent.get(intent)

        # ========== 情况1：景点推荐 ===========
        # 景点推荐不需要调用子代理，直接让大模型生成推荐内容
        # 但会先查询目的地天气，确保推荐基于真实天气而非虚构
        if intent == "attraction":
            chain = SmartVoyagePrompts.attraction_prompt() | self.llm
            weather_info = await self._fetch_weather_for_attraction(query_str)
            return chain.invoke({"query": query_str, "weather_info": weather_info}).content.strip()

        # ========== 情况2：需要调用子代理 ===========
        elif agent_name:
            # 对于票务查询类意图，提取关键实体（城市、日期等）保存到记忆中
            # 这样后续对话可以引用这些实体（多轮对话场景）
            # extract_entities 内部已自动持久化到数据库
            if intent in ["flight", "train", "concert", "car_rental", "tour_group", "insurance"]:
                self.memory.extract_entities(intent, query_str)
                self.memory.update_task_context({"type": intent, "query": query_str})

            # 从代理网络中获取对应的 agent 实例
            agent = self.agent_registry.get_agent(agent_name)
            if agent is None:
                # agent 未注册或不可用时，返回友好提示
                logger.warning(f"未找到代理：{agent_name}")
                return f"抱歉，{agent_name} 暂时不可用，请稍后重试。"

            # 构建发送给 agent 的消息：结构化传递对话历史和当前查询
            agent_input = json.dumps({
                "history": self.memory.get_short_term_text(),
                "query": query_str,
                "current_date": self._today(),
                "guardrail": "历史只用于补全上下文；日期判断以 current_date 和当前 query 为准，业务结论只能来自工具结果。",
            }, ensure_ascii=False)
            msg = Message(content=TextContent(text=agent_input), role=MessageRole.USER)
            task = Task(id="task-" + str(uuid.uuid4()), message=msg.to_dict())

            # 通过 A2A 协议异步发送任务给子代理
            # run_in_executor 将异步操作放到线程池中执行（避免阻塞事件循环）
            try:
                raw_response = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: asyncio.run(agent.send_task_async(task))
                )
                logger.info(f"{agent_name} 原始响应: {raw_response}")

                # 解析 agent 返回结果
                if raw_response.artifacts:
                    # 优先读取 artifact；成功结果和失败原因都会放在这里。
                    agent_result = raw_response.artifacts[0]['parts'][0]['text']
                elif raw_response.status.message:
                    # 有消息但可能失败了，尝试从消息中提取文本
                    agent_result = raw_response.status.message.get('content', {}).get('text', str(raw_response.status.message))
                else:
                    agent_result = f"查询失败：{raw_response.status.message or '未知错误'}"
            except Exception as e:
                # agent 服务不可用（如网络不通、服务崩溃），返回友好错误提示
                logger.error(f"{agent_name} 调用异常: {str(e)}")
                return f"{agent_name} 服务暂时不可用：{str(e)}"

            # 根据代理类型，使用对应的总结 prompt 处理结果
            expert_result = self._parse_expert_result(agent_name, agent_result)
            if agent_name == "WeatherQueryAssistant":
                chain = SmartVoyagePrompts.summarize_weather_prompt() | self.llm
                if expert_result.unavailable:
                    return self._fallback_service_reply(agent_name, expert_result.to_json())
                return chain.invoke({
                    "query": query_str,
                    "raw_response": expert_result.summary_input(),
                    "current_date": self._today(),
                }).content.strip()
            elif agent_name in ("TicketQueryAssistant", "TicketAssistant"):
                chain = SmartVoyagePrompts.summarize_ticket_prompt() | self.llm
                if expert_result.unavailable:
                    return self._fallback_service_reply(agent_name, expert_result.to_json())
                return chain.invoke({
                    "query": query_str,
                    "raw_response": expert_result.summary_input(),
                    "current_date": self._today(),
                }).content.strip()
            elif agent_name == "TripAssistant":
                chain = SmartVoyagePrompts.summarize_ticket_prompt() | self.llm
                if expert_result.unavailable:
                    return self._fallback_service_reply(agent_name, expert_result.to_json())
                return chain.invoke({
                    "query": query_str,
                    "raw_response": expert_result.summary_input(),
                    "current_date": self._today(),
                }).content.strip()
            else:
                # 其他代理直接返回原始结果
                return agent_result

        # ========== 情况3：未识别的意图 ===========
        else:
            return "暂不支持此意图。"

    def _should_use_collaborative_planning(self, intents: list, user_input: str) -> bool:
        """委托协作引擎判断是否进入 Planner/Critic/Summary 链路。"""
        return self.collaboration_engine.should_use_collaborative_planning(
            intents, user_input
        )

    async def collaborative_plan(
        self,
        user_input: str,
        intents: list,
        user_queries: dict,
        run_meta: dict | None = None,
    ) -> str:
        """委托协作引擎执行局部强多智能体协作。"""
        return await self.collaboration_engine.plan(
            user_input,
            intents,
            user_queries,
            run_meta=run_meta,
        )

    async def execute_step(self, step: dict, user_queries: dict) -> str:
        """
        执行单个计划步骤（供 ReAct 循环调用）

        这个方法负责将规划中的单个步骤转化为实际的执行：
        1. 从步骤中提取意图和查询内容
        2. 调用 _call_agent_intent 执行实际的查询

        参数：
            step (dict): 计划中的单个步骤，格式如：
                {"step": 1, "action": "查询天气", "intent": "weather", "depends_on": 0}
            user_queries (dict): 改写后的查询字典

        返回值：
            str: 该步骤的执行结果
        """
        intent = step.get("intent", "")
        # 优先从 user_queries 中获取改写后的查询，如果没有则用用户原始输入
        query_str = user_queries.get(intent, self.messages[-1]["content"] if self.messages else "")
        logger.info(f"执行步骤：{step.get('action', '')}，意图：{intent}")

        # 委托给统一的 agent 调用方法
        return await self._call_agent_intent(intent, query_str)

    async def react_loop(self, steps: list, user_queries: dict) -> str:
        """
        ReAct 循环：按规划步骤逐步执行，直接行动（Action），跳过 Thought 推理

        执行流程：
        1. 按依赖关系分组步骤
        2. 无依赖的步骤并行执行
        3. 记录每步结果（Observation）
        4. 最终汇总生成连贯回复

        优化：省略 Thought LLM 调用（plan 已确定动作，Thought 无额外决策价值），
        省去每步 1 次 LLM 调用，多步骤场景下显著减少响应时间。

        参数：
            steps (list): 计划步骤列表，如：
                [{"step": 1, "action": "...", "intent": "flight", "depends_on": 0},
                 {"step": 2, "action": "...", "intent": "weather", "depends_on": 0}]
            user_queries (dict): 改写后的查询字典

        返回值：
            str: 综合所有步骤结果的最终回复
        """
        observations = []     # 存储每个步骤的执行结果（Observation）
        step_results = []     # 存储每个步骤的详细结果（步骤号、描述、结果）

        # 按依赖关系分组：depends_on 值相同的步骤可以并行
        from collections import OrderedDict
        dep_groups = OrderedDict()
        for step in steps:
            dep = step.get("depends_on", 0)
            dep_groups.setdefault(dep, []).append(step)

        # 逐组执行，组内并行
        for dep_key, group_steps in dep_groups.items():
            # 组内步骤并行执行 Action
            if len(group_steps) > 1:
                logger.info(f"并行执行 {len(group_steps)} 个无依赖步骤")
                tasks = [self.execute_step(s, user_queries) for s in group_steps]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for step, result in zip(group_steps, results):
                    step_num = step.get("step", 0)
                    step_desc = step.get("description", step.get("action", ""))
                    if isinstance(result, Exception):
                        result = f"执行失败：{result}"
                    observations.append(result)
                    step_results.append({"step": step_num, "description": step_desc, "result": result})
                    logger.info(f"ReAct 步骤 {step_num} 结果: {result[:100]}...")
            else:
                # 单步骤，直接执行
                step = group_steps[0]
                step_num = step.get("step", 0)
                step_desc = step.get("description", step.get("action", ""))
                result = await self.execute_step(step, user_queries)
                observations.append(result)
                step_results.append({"step": step_num, "description": step_desc, "result": result})
                logger.info(f"ReAct 步骤 {step_num} 结果: {result[:100]}...")

        # ========== 所有步骤完成后的最终汇总 ==========
        if len(step_results) > 1:
            # 多个步骤：使用专门的汇总 prompt，将各步骤结果整合成一条连贯回复
            summary_chain = SmartVoyagePrompts.react_summary_prompt() | self.llm
            all_obs = "\n".join([f"步骤{s['step']} ({s['description']}): {s['result']}" for s in step_results])
            final_response = summary_chain.invoke({
                "query": self.messages[-1]["content"] if self.messages else "",
                "all_observations": all_obs,
                "current_date": self._today(),
            }).content.strip()
        else:
            # 只有一个步骤：直接使用该步骤的结果
            final_response = observations[0] if observations else "暂无结果"

        return final_response

    async def chat(self, user_input: str) -> str:
        """
        处理用户输入的非流式入口。

        ChatService 只负责承接调用，具体状态流由 LangGraph 执行。
        """
        # Flow step 3: ChatService 是门面，真正的流程推进委托给 LangGraph。
        return await self.workflow_graph.chat(user_input)

    def get_agent_cards(self) -> list:
        """
        获取所有代理的卡片信息

        Agent Card 是 A2A 协议中描述一个 agent 能力的方式，
        包含：名称、技能列表、描述、地址、状态等。

        返回值：
            list: 代理卡片列表，每个卡片格式为：
                {
                    "name": "WeatherQueryAssistant",
                    "skills": ["天气查询: 根据城市查询天气"],
                    "description": "天气查询助手",
                    "url": "http://127.0.0.1:5005",
                    "status": "在线"
                }
        """
        return self.agent_registry.get_agent_cards()

    def get_memory_state(self) -> dict:
        """
        获取当前记忆状态的摘要信息（用于调试和展示）

        返回值：
            dict: 包含以下信息：
                - short_term_messages: 最近5条对话消息
                - user_profile: 用户偏好
                - current_task: 当前任务上下文
                - entity_history: 最近5条实体记录
        """
        return {
            "short_term_messages": [
                {"role": "用户" if m["role"] == "user" else "助手", "content": m["content"], "timestamp": m["timestamp"]}
                for m in self.memory.short_term_messages[-5:]
            ],
            "user_profile": self.memory.user_profile,
            "current_task": self.memory.current_task,
            "entity_history": self.memory.entity_history[-5:]
        }

    def clear_memory(self):
        """
        清空所有记忆（短期记忆、用户偏好、任务上下文等）

        用户输入 'clear' 命令时调用，用于重置对话状态。
        memory.clear() 内部会自动清理数据库中的持久化数据。
        """
        self.memory.clear()
        self.messages.clear()
        self.conversation_history = ""
        self.trace_recorder.reset()

    async def chat_stream(self, user_input: str):
        """
        以 SSE 兼容方式处理用户输入，具体状态流由 LangGraph 执行。
        """
        async for chunk in self.workflow_graph.chat_stream(user_input):
            yield chunk
