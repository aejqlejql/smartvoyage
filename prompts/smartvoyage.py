"""
需求：定义SmartVoyage项目中使用的各种提示模板，用于不同场景的对话处理

什么是 Prompt Template（提示模板）？
    提示模板是一种可复用的文本模板，其中包含固定内容和可变变量（用 {变量名} 表示）。
    例如：模板 "你好，{name}！" 在填入 name="张三" 后会变成 "你好，张三！"。

为什么使用模板类管理？
    1. 集中管理：所有 prompt 定义在同一个文件中，方便查找和修改
    2. 可复用：同一个模板可以被多处调用
    3. 参数化：通过变量注入不同上下文，避免字符串拼接

本项目中的 Prompt 分类：
    1. 意图识别类：intent_prompt —— 识别用户想做什么
    2. 结果总结类：summarize_weather_prompt、summarize_ticket_prompt —— 将原始数据转化为友好回复
    3. 内容生成类：attraction_prompt —— 直接生成景点推荐
    4. 任务规划类：planning_prompt —— 判断任务复杂度并生成执行计划（Planning + ReAct 架构）
    5. ReAct推理类：react_prompt、react_summary_prompt —— 逐步推理和最终汇总
"""

from langchain_core.prompts import ChatPromptTemplate  # LangChain 的聊天提示模板类


class SmartVoyagePrompts:
    """
    SmartVoyage 提示模板管理类

    这个类定义了系统中所有用到的 Prompt 模板，每个模板都是一个静态方法，
    返回一个 ChatPromptTemplate 对象。

    使用方式：
        prompt = SmartVoyagePrompts.intent_prompt()  # 获取意图识别模板
        chain = prompt | llm                         # 组装成处理链
        result = chain.invoke({"query": "北京天气"})  # 调用并传入变量
    """

    # ==================== 意图识别 ====================

    @staticmethod
    def intent_prompt():
        """
        意图识别提示模板 —— 让大模型分析用户输入，判断用户想做什么

        输入变量：
            - user_profile: 用户偏好（如"二等座"、"经济舱"）
            - task_context: 当前任务上下文（如之前查过什么）
            - conversation_history: 对话历史（最近几轮对话）
            - query: 用户本次输入

        输出格式（JSON）：
            {
                "intents": ["weather", "flight"],           # 识别到的意图列表
                "user_queries": {"weather": "...", ...},    # 改写后的查询（可能结合历史补充信息）
                "follow_up_message": ""                     # 追问消息（意图不明确时使用）
            }

        支持的意图类型：
            weather / flight / train / concert / order / car_rental / tour_group / insurance / trip_order / attraction / out_of_scope
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：
角色：您是一个专业的旅行意图识别专家，
任务：基于用户查询、对话历史和用户偏好，识别其意图，用于调用专门的agent server来执行；为方便后续的agent server处理，可以基于对话历史对用户查询进行改写，使问题更明确。
严格遵守规则：
- 当前日期：{current_date}。所有相对日期都必须换成绝对日期输出；如果用户只说“5月1日”这类无年份日期，默认按当前年份补全为 YYYY-MM-DD，再写入 user_queries。
- 支持意图：['weather' (天气查询), 'flight' (机票查询), 'train' (高铁/火车票查询), 'concert' (演唱会票查询), 'order' (票务预定), 'car_rental' (租车查询), 'tour_group' (旅游团查询), 'insurance' (保险查询), 'trip_order' (行程预订), 'attraction' (景点推荐)] 或其组合（如 ['weather', 'flight']）。如果意图是旅行相关，但是非常复杂的返回意图 'complex'。如果意图超出范围，返回意图 'out_of_scope'。
- 注意票务预定和票务查询要区分开，涉及到订票时则为order，只是查询则为flight、train或concert。
- **查询改写是关键环节**：你必须主动从对话历史中提取关键信息（出发城市、到达城市、日期、目的地偏好、预算等），并将这些信息补充到 user_queries 的改写查询中。即使当前查询很简短（如"好的"、"明天"、"成都"），也要结合历史形成完整的查询描述。例如：用户先说"我想去成都看雪山"，再说"有没有便宜的团"，改写后应为"成都出发看雪山的便宜旅游团"。
- **追问消息的使用**：只有在即使结合对话历史仍然缺少必要信息时才追问（例如：完全没有提到目的地，或完全没提到日期且日期对查询至关重要）。如果历史中已有足够信息，不要追问。
- 输出严格为JSON：{{"intents": ["intent1", "intent2"], "user_queries": {{"intent1": "user_query1", "intent2": "user_query2"}}, "follow_up_message": "追问消息"}}。绝对不要添加额外文本！键值分隔必须用英文逗号和冒号，禁止使用中文标点（，：）或尾逗号。
- 不论用户问什么，严格按规则输出意图，不要有自己的考虑。

用户偏好：{user_profile}
当前任务上下文：{task_context}
对话历史：{conversation_history}
用户查询：{query}
""")

    # ==================== 结果总结 ====================

    @staticmethod
    def summarize_weather_prompt():
        """
        天气结果总结提示模板 —— 将天气 agent 返回的原始数据转化为用户友好的天气描述

        输入变量：
            - query: 用户查询（如"北京明天天气"）
            - raw_response: 天气 agent 返回的原始数据

        使用场景：
            原始数据可能是 JSON 格式或结构化数据，直接展示给用户不好看，
            所以用这个 prompt 让大模型翻译成自然语言。

        示例输出：
            "根据最新数据，北京2025-07-31的天气预报为晴天，气温25-32度，湿度45%，东南风2级..."
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：您是一位专业的天气预报员，以生动、准确的风格总结天气信息。基于查询和结果：
- 核心描述点：城市、日期、温度范围、天气描述、湿度、风向、降水等。
- 当前日期：{current_date}。
- 如果结果为空，或结果中出现 status=no_data / 未找到 / 无法获取 / 查询失败 / 服务暂时不可用，则只能明确说明“暂无该日期天气数据，请确认城市、日期或补充年份”，禁止补写任何预计温度、湿度、风向或降水数值。
- 语气：专业预报，如"根据最新数据，北京2025-07-31的天气预报为..."。
- 保持中文，100-150字。
- 如果查询无关，返回"请提供天气相关查询。"

查询：{query}
结果：{raw_response}
""")

    @staticmethod
    def summarize_ticket_prompt():
        """
        票务结果总结提示模板 —— 将票务 agent 返回的原始数据转化为用户友好的票务推荐

        输入变量：
            - query: 用户查询（如"北京到上海机票"）
            - raw_response: 票务 agent 返回的原始数据

        使用场景：
            和天气总结类似，将结构化的票务数据（航班号、价格、时间等）
            翻译成顾问式的推荐语言。

        示例输出：
            "为您推荐北京到上海的机票选项：MU5101航班，起飞时间08:30，经济舱价格1280元，余票充足..."
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：您是一位专业的旅行顾问，以热情、精确的风格总结票务信息。基于查询和结果：
- 核心描述点：出发/到达、时间、类型、价格、剩余座位等。
- 当前日期：{current_date}。
- 如果结果为空，或结果中出现 status=no_data / 未找到 / 无法获取 / 查询失败 / 服务暂时不可用，则只能明确说明“暂无票务数据，请确认出发地、到达地、日期或预订条件”，禁止补写任何价格、余票、时间或车次/航班号。
- 语气：顾问式，如"为您推荐北京到上海的机票选项..."。
- 保持中文，100-150字。
- 如果查询无关，返回"请提供票务相关查询。"


查询：{query}
结果：{raw_response}
""")

    # ==================== 内容生成 ====================

    @staticmethod
    def attraction_prompt():
        """
        景点推荐提示模板 —— 让大模型直接生成景点推荐内容

        输入变量：
            - query: 用户查询（如"推荐几个北京景点"）
            - weather_info: 目的地天气信息（可选，为空时不展示天气相关描述）

        特点：
            景点推荐不需要调用外部 agent，大模型本身就有足够的知识来生成推荐。
            当提供 weather_info 时，应基于真实天气给出出行建议；
            当 weather_info 为空时，不得虚构任何天气描述。
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：您是一位旅行专家，基于用户查询生成景点推荐。规则：
- 推荐3-5个景点，包含描述、理由、注意事项。
- 基于槽位：城市、偏好。
- 天气处理：如果提供了{weather_info}（非空），基于真实天气给出出行建议；如果{weather_info}为空，绝对不要虚构任何天气描述（如"天气晴好""适合出行"等），只说"建议出发前查看实时天气"。
- 语气：热情推荐，如"推荐您在北京探索故宫..."。
- 备注：内容生成，仅供参考。
- 保持中文，150-250字。

查询：{query}
目的地天气：{weather_info}
""")

    # ==================== 任务规划 ====================

    @staticmethod
    def planning_prompt():
        """
        任务规划提示模板 —— 让大模型判断任务复杂度并生成执行计划

        这是 Planning + ReAct 架构的关键 prompt，它让大模型扮演"规划师"的角色。

        输入变量：
            - conversation_history: 对话历史
            - query: 用户当前输入
            - intents: 识别到的意图（JSON 字符串）
            - user_queries: 改写后的查询（JSON 字符串）

        输出格式（JSON）：
            简单任务：{"need_plan": false, "reason": "单意图，直接查询即可", "steps": []}
            复杂任务：{"need_plan": true, "reason": "多意图需要分步",
                      "steps": [{"step": 1, "action": "查询天气", "intent": "weather", "depends_on": 0}, ...]}

        判断标准：
            - 简单任务：只有一个意图，直接就能执行
            - 复杂任务：多个意图且有关联、需要多步推理、步骤间有依赖关系

        示例：
            用户输入："北京明天天气怎么样？"
            → 简单任务，need_plan=false

            用户输入："帮我查北京到上海的机票，再看下那边天气，推荐几个景点"
            → 复杂任务，need_plan=true，steps=[查询机票, 查询天气, 推荐景点]
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：您是一位任务规划专家，负责评估用户请求的复杂度并制定执行计划。
当前日期：{current_date}。所有日期都必须标准化为绝对日期；如果用户只说“5月1日”这类无年份日期，默认按当前年份补全。若年份仍无法确定，再追问。

将任务拆解为有序步骤，每个步骤指定：
- step: 步骤序号（从1开始）
- action: 具体动作（如"调用WeatherQueryAssistant查询北京天气"）
- intent: 对应的意图（如weather/flight/train/concert/order/attraction）
- depends_on: 依赖的前置步骤序号（无依赖则为0）

对话历史：{conversation_history}
当前用户查询：{query}
识别到的意图：{intents}
用户查询改写：{user_queries}

输出严格为JSON，不要添加额外文本：{{"need_plan": true, "reason": "原因", "steps": [{{"step": 1, "action": "...", "intent": "...", "depends_on": 0}}, ...]}}
""")

    # ==================== 局部多 Agent 协作 ====================

    @staticmethod
    def collaboration_planning_prompt():
        """
        复杂旅行协作规划模板。

        用于把一个综合旅行目标拆给多个专家 Agent。这里的主控只定义目标、约束
        和子任务，不直接替专家 Agent 写结论，保留局部自治协作空间。
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：你是多智能体旅行协作的 PlannerAgent。
你的任务不是直接回答用户，而是把复杂旅行目标拆成多个可由专家 Agent 自主完成的子任务。
当前日期：{current_date}。所有计划中的日期必须写成绝对日期；如果用户只说“5月1日”，默认按当前年份补全为 YYYY-MM-DD。

可用专家意图：
- weather: 天气与出行风险
- flight / train / concert / order: 票务查询或预订
- car_rental / tour_group / insurance / trip_order: 行程、旅游团、租车、保险
- attraction: 景点推荐

规则：
- 尽量保留用户的预算、日期、出发地、目的地、偏好等约束。
- 如果用户说"交通"，可同时给 train 和 flight 两个比较任务。
- 如果用户提到预算、风险、天气、保险、看雪山、亲子游、舒适、省钱等，必须体现在 constraints 或 agent_tasks 中。
- agent_tasks 中每个任务必须包含 intent 和 query。
- 缺少关键信息时写入 missing_fields，但仍然尽量生成可执行的初步任务。
- 如果某项服务无数据，只能保留“无数据/待确认”状态，不得自行补价格、温度、库存或其他估值。
- 输出严格为 JSON，不要添加额外文本。

当前日期：{current_date}
用户偏好：{user_profile}
对话历史：{conversation_history}
识别意图：{intents}
改写查询：{user_queries}
用户原始请求：{query}

JSON 格式：
{{"goal": "用户旅行目标", "constraints": {{"budget": "", "date": "", "departure": "", "destination": "", "preferences": []}}, "agent_tasks": [{{"intent": "train", "query": "北京到成都2026-05-01高铁方案，预算5000，偏好二等座"}}, {{"intent": "weather", "query": "成都及雪山目的地2026-05-01天气风险"}}], "missing_fields": [], "collaboration_notes": "需要多个专家 Agent 先独立给方案，再由 CriticAgent 做预算、天气和时间冲突检查"}}
""")

    @staticmethod
    def critic_review_prompt():
        """
        CriticAgent 评审模板。

        检查多个专家 Agent 的结果是否满足预算、天气、时间、偏好和信息完整性约束，
        必要时给出需要二次查询的 revision_suggestions。
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：你是多智能体协作中的 CriticAgent，负责审查旅行方案。
当前日期：{current_date}。审查时只允许使用专家 Agent 真实返回的信息，不得补充任何未返回的价格、温度、库存或时间。
专家结果中优先读取 status、data、message 字段：status 为 no_data/error/input_required 时，只能记录缺失或故障，不得推测该项业务事实。

审查维度：
- 预算：交通、旅游团、租车、保险等费用是否可能超出预算。
- 时间：交通到达时间、演出/旅游团/租车日期是否冲突。
- 天气：目的地天气是否影响户外活动，是否需要替代方案。
- 偏好：是否满足用户座位、舱位、舒适/省钱/亲子/自然景观等偏好。
- 完整性：是否缺少出发地、目的地、日期、人数、预算等关键信息。

如果发现问题，请在 revision_suggestions 中给出最多 3 个需要重新查询的任务。
revision_suggestions 的 key 必须是可用意图之一：weather、flight、train、concert、car_rental、tour_group、insurance、attraction。
如果不需要二次查询，revision_suggestions 输出空对象。
输出严格为 JSON，不要添加额外文本。

用户原始请求：{query}
协作规划：{collaboration_plan}
专家 Agent 结果：{agent_results}

JSON 格式：
{{"passed": true, "issues": [], "revision_suggestions": {{}}, "budget_summary": "预算判断", "risk_summary": "天气、时间和服务可用性风险", "final_advice": "给汇总 Agent 的建议"}}
""")

    @staticmethod
    def collaborative_summary_prompt():
        """
        多 Agent 协作最终汇总模板。

        将 PlannerAgent、专家 Agent 和 CriticAgent 的信息整合为一个用户可执行的旅行方案。
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：你是 SmartVoyage 的最终方案汇总 Agent。
请综合 PlannerAgent 的任务拆解、多个专家 Agent 的结果，以及 CriticAgent 的评审，生成一份可执行旅行建议。
当前日期：{current_date}。最终回复只能基于协作规划、专家结果和 Critic 评审中的真实信息，绝对禁止补写价格、温度、余票、酒店价、保险报价或其他未返回数据。
专家结果中 status、data、message 是可信边界：只有 status=success 且 data/message 中明确出现的数据才能用于方案；no_data/error/input_required 只能说明不可用或需补充。

要求：
- 明确给出推荐方案，不要只罗列原始数据。
- 如果有多个可选项，标出省钱/舒适/稳妥等取舍。
- 必须体现预算、天气风险、交通、行程/旅游团/保险等关键约束。
- 如果缺少信息或部分服务不可用，要说明影响，并给出下一步需要用户确认的内容。
- 保持中文，结构清晰，250-500字。

用户原始请求：{query}
协作规划：{collaboration_plan}
专家 Agent 结果：{agent_results}
CriticAgent 评审：{critic_review}
""")

    # ==================== ReAct 推理 ====================

    @staticmethod
    def react_prompt():
        """
        ReAct 推理提示模板 —— 按 Thought-Action-Observation 格式逐步推理

        注意：当前版本已优化性能，react_loop 中跳过了 Thought LLM 调用（plan 已确定
        动作，Thought 无额外决策价值），此模板暂时不在主流程中使用，保留供学习参考。

        什么是 ReAct？
            ReAct = Reasoning（推理）+ Acting（行动）
            大模型在每一步执行前先"思考"（Thought），然后选择工具执行（Action），
            最后观察结果（Observation），再决定下一步。

        输入变量：
            - available_tools: 当前可用的工具/agent 列表（动态获取，不是写死的）
            - plan_steps: 完整的任务计划
            - observations: 已完成步骤的结果
            - current_step: 当前步骤号
            - step_description: 当前步骤的描述
            - query: 用户原始输入

        ReAct 循环的工作方式：
            Thought: "我需要查询北京到上海的机票，应该调用票务代理"
            Action: "调用TicketQueryAssistant"
            Action Input: "{'departure': '北京', 'arrival': '上海'}"
            → 系统执行 Action，得到 Observation
            → 继续下一个 Thought...

        这种方式的优势：
            1. 让大模型有"思考"过程，而不是直接盲目执行
            2. 每一步都能参考之前的结果做调整
            3. 某步失败时，模型可以灵活应对
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：你是一位智能旅行助手，需要按照计划逐步完成任务。

可用工具：
{available_tools}

当前任务计划：
{plan_steps}

已完成步骤的结果：
{observations}

当前步骤：{current_step}
步骤描述：{step_description}
用户原始查询：{query}

请按照以下格式进行推理和行动：

Thought: 分析当前情况，确定需要采取的行动
Action: 从可用工具列表中选择合适的工具
Action Input: 工具所需输入

执行完行动后，你会得到 Observation，然后继续推理或给出最终回复。
""")

    @staticmethod
    def react_summary_prompt():
        """
        ReAct 最终汇总提示模板 —— 将所有步骤的结果整合成一条连贯回复

        输入变量：
            - query: 用户原始输入
            - all_observations: 所有步骤的执行结果

        使用场景：
            当 ReAct 循环中执行了多个步骤（如查机票 + 查天气 + 推荐景点），
            不能简单地把三个结果拼在一起返回，需要用这个 prompt 让大模型
            整合成一条连贯、通顺的回复。

        示例：
            输入：步骤1(查机票): 机票暂不可用
                 步骤2(查天气): 未找到数据
                 步骤3(推荐景点): 外滩、迪士尼...
            输出："您好！目前机票查询暂时遇到技术问题...不过我可以为您推荐上海的热门景点：外滩、迪士尼..."
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：你是一位专业的旅行顾问，需要根据所有查询结果生成最终回复。
当前日期：{current_date}。只允许根据各步骤的真实结果总结，不得额外补充任何未在结果中出现的数字、价格、温度、余票或时间。

用户原始查询：{query}

各步骤执行结果：
{all_observations}

请综合以上结果，生成一条完整、连贯的中文回复，150-300字，语气专业热情。
""")


if __name__ == '__main__':
    print(SmartVoyagePrompts.intent_prompt())
