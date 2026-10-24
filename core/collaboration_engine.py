"""复杂旅行目标的强多智能体协作引擎。

CollaborationEngine 只处理“综合型旅行方案”这一类任务。它不负责普通查天气、
查票等简单请求，也不直接知道 FastAPI 或前端。

协作链路：

1. PlannerAgent：把用户综合目标拆成多个专家 Agent 可执行的子任务。
2. Expert Agents：并行调用天气、票务、行程、景点等专家能力。
3. CriticAgent：审查结果是否满足预算、天气、时间、完整性等约束。
4. Revision Round：根据 CriticAgent 建议最多追加一轮修正查询。
5. SummaryAgent：把所有结果汇总成用户可执行的最终方案。

本类通过 ``call_agent_intent`` 回调调用现有 Agent 路由，因此不直接依赖
ChatService 的内部实现，也便于单独测试协作逻辑。
"""

import asyncio
import json

from ..contracts.expert_result import ExpertResult
from ..infrastructure.logging import logger
from ..prompts.smartvoyage import SmartVoyagePrompts
from .guardrails import (
    all_results_unavailable,
    current_date_str,
    current_year,
    normalize_query_payload,
)
from .message_utils import parse_llm_json, query_to_text


class CollaborationEngine:
    """执行 Planner -> Expert -> Critic -> Summary 的协作状态链。"""

    def __init__(
        self,
        llm,
        memory,
        call_agent_intent,
        trace_recorder,
        intent_map: dict,
    ):
        """注入协作链执行所需的模型、记忆、Agent 调用回调和 trace 记录器。"""
        # llm/memory/trace_recorder 由 ChatService 装配后注入。
        # call_agent_intent 是异步回调，负责把具体 intent 交给已有 A2A/MCP 链路执行。
        self.llm = llm
        self.memory = memory
        self.call_agent_intent = call_agent_intent
        self.trace_recorder = trace_recorder
        self.intent_map = intent_map

    def should_use_collaborative_planning(self, intents: list, user_input: str) -> bool:
        """判断本次请求是否应进入"局部强多智能体协作"链路。

        简单意图（查天气、查票）走普通 direct/ReAct 路径即可；只有当用户请求
        表现出"需要一份综合旅行方案"的特征时，才进入 PlannerAgent → 专家 Agent →
        CriticAgent → SummaryAgent 的协作链。

        判定规则（按优先级，命中即返回 True）：
        1. intent_agent 明确返回了 ``complex``：LLM 已判断普通单工具无法完成。
        2. 可执行意图数量 >= 3：如"交通 + 天气 + 保险"这类综合目标。
        3. 可执行意图 >= 2 且出现"预算/方案/行程/亲子/省钱"等强约束词。
        4. 出现"规划/安排/一站式"等宽泛请求词，且同时出现交通/天气/景点等旅行领域词。

        参数：
            intents: 意图识别结果列表，如 ["weather", "train", "insurance"]。
            user_input: 用户原始输入文本。

        返回值：
            bool: True 表示进入协作链；False 表示走普通链路。
        """
        # 只有系统已知的可执行意图才参与复杂度判断；out_of_scope 等意图不应触发协作。
        executable_intents = [
            intent for intent in intents
            if intent in self.intent_map or intent == "attraction"
        ]
        if "complex" in intents:
            # intent_agent 如果明确给出 complex，说明它已经判断该请求无法靠普通单工具完成。
            return True
        if len(executable_intents) >= 3:
            # 三个及以上可执行意图通常代表综合旅行目标，例如交通 + 天气 + 保险。
            return True

        # 多意图 + 强约束词通常意味着用户需要“一份方案”，而不是孤立查询结果。
        collaboration_keywords = (
            "规划", "安排", "方案", "行程", "预算", "全套", "一站式",
            "看雪山", "亲子", "省钱", "舒适", "风险", "保险", "租车", "旅游团",
        )
        if len(executable_intents) >= 2 and any(
            keyword in user_input for keyword in collaboration_keywords
        ):
            # 两个以上意图同时出现“预算/方案/行程”等词，用户期待的是协调后的方案。
            return True

        broad_request_keywords = ("规划", "安排", "一站式", "全套", "预算")
        travel_domain_keywords = (
            "交通", "机票", "火车", "高铁", "天气", "景点", "旅游团", "保险", "租车"
        )
        return (
            any(keyword in user_input for keyword in broad_request_keywords)
            and any(keyword in user_input for keyword in travel_domain_keywords)
        )

    def _normalize_plan(
        self,
        plan: dict,
        user_input: str,
        user_queries: dict,
    ) -> dict:
        """校验并归一化 PlannerAgent 输出。

        PlannerAgent 是 LLM 角色，输出可能存在别名、重复任务或无效 intent。
        这里把别名映射到系统真实 intent，去重，并保证最终至少有一个可执行子任务。
        """
        if not isinstance(plan, dict):
            raise ValueError("PlannerAgent 必须返回 JSON 对象")

        # 允许 Planner 用自然语言或宽泛分类表达任务，再映射到系统内部 intent。
        alias_map = {
            "ticket": "train",
            "tickets": "train",
            "transport": "train",
            "trip": "tour_group",
            "travel": "tour_group",
            "weather_risk": "weather",
        }
        valid_intents = set(self.intent_map.keys()) | {"attraction"}
        raw_tasks = plan.get("agent_tasks", [])
        normalized_tasks = []
        seen = set()

        if isinstance(raw_tasks, dict):
            # 兼容 Planner 输出 {"weather": "...", "train": "..."} 的简写形式。
            raw_tasks = [
                {"intent": intent, "query": query}
                for intent, query in raw_tasks.items()
            ]

        for task in raw_tasks if isinstance(raw_tasks, list) else []:
            if not isinstance(task, dict):
                # 非对象任务无法执行，直接忽略，由最终空任务校验兜底报错。
                continue
            intent = str(task.get("intent", "")).strip()
            intent = alias_map.get(intent, intent)
            if intent not in valid_intents:
                # Planner 可能生成系统当前不支持的 intent。这里不隐式调用未知能力。
                continue
            query_text = query_to_text(
                task.get("query") or user_queries.get(intent) or user_input
            )
            query_text = normalize_query_payload(
                query_text,
                default_year=current_year(),
            )
            key = (intent, query_text)
            if key in seen:
                # 相同 intent + query 的任务只执行一次，避免重复调用专家 Agent。
                continue
            seen.add(key)
            normalized_tasks.append({"intent": intent, "query": query_text})

        if not normalized_tasks:
            raise ValueError("PlannerAgent 输出缺少可执行的 agent_tasks")

        plan.setdefault("goal", user_input)
        plan.setdefault("constraints", {})
        plan.setdefault("missing_fields", [])
        plan.setdefault("collaboration_notes", "")
        plan["agent_tasks"] = normalized_tasks
        return plan

    def _build_plan(
        self,
        user_input: str,
        intents: list,
        user_queries: dict,
    ) -> dict:
        """调用 PlannerAgent 生成专家 Agent 子任务。"""
        chain = SmartVoyagePrompts.collaboration_planning_prompt() | self.llm
        current_date = current_date_str()

        plan_response = chain.invoke({
            "current_date": current_date,
            "user_profile": self.memory.get_profile_text(),
            "conversation_history": self.memory.get_short_term_text(),
            "query": normalize_query_payload(
                user_input,
                default_year=current_year(),
            ),
            "intents": json.dumps(intents, ensure_ascii=False),
            "user_queries": json.dumps(
                normalize_query_payload(user_queries, default_year=current_year()),
                ensure_ascii=False,
            ),
        }).content.strip()
        logger.info(f"协作规划原始响应: {plan_response}")
        # PlannerAgent 必须返回 JSON；解析失败会向上抛出，由 LangGraph error 节点统一收口。
        plan = parse_llm_json(plan_response)

        return self._normalize_plan(plan, user_input, user_queries)

    async def _execute_task(
        self,
        task: dict,
        round_no: int = 1,
    ) -> dict:
        """执行一个专家 Agent 子任务，并返回 trace 需要的结构化结果。"""
        intent = task.get("intent", "")
        query = query_to_text(task.get("query", ""))
        agent_name = self.intent_map.get(intent, "AttractionGenerator")
        try:
            # call_agent_intent 复用 ChatService 既有路由：attraction 走 LLM，
            # 其他 intent 走 A2A 专家 Agent。
            result = await self.call_agent_intent(intent, query)
        except Exception as exc:
            logger.exception(f"协作子任务失败: {intent}")
            result = f"执行失败：{exc}"
        expert_result = ExpertResult.from_text(
            result,
            agent=agent_name,
            source="collaboration",
        )
        return {
            # 这里返回的是 trace-friendly 结构，不是直接给用户看的最终答案。
            "round": round_no,
            "intent": intent,
            "agent": agent_name,
            "query": query,
            "status": expert_result.status,
            "data": expert_result.data if expert_result.data is not None else [],
            "message": expert_result.message,
            "error_code": expert_result.error_code,
            "result": result,
        }

    def _review(
        self,
        user_input: str,
        collaboration_plan: dict,
        agent_results: list,
    ) -> dict:
        """调用 CriticAgent 审查专家结果。

        CriticAgent 不直接回答用户，而是输出结构化审查结果和可选修正建议。
        """
        chain = SmartVoyagePrompts.critic_review_prompt() | self.llm
        review_response = chain.invoke({
            "current_date": current_date_str(),
            "query": user_input,
            "collaboration_plan": json.dumps(
                collaboration_plan, ensure_ascii=False
            ),
            "agent_results": json.dumps(agent_results, ensure_ascii=False),
        }).content.strip()
        logger.info(f"CriticAgent 原始响应: {review_response}")
        review = parse_llm_json(review_response)
        if not isinstance(review.get("revision_suggestions", {}), dict):
            # revision_suggestions 必须是 {intent: query}，否则无法安全转成修正任务。
            raise ValueError("CriticAgent 的 revision_suggestions 必须是对象")
        return review

    async def _run_revision_tasks(self, review: dict) -> list:
        """根据 CriticAgent 建议执行一轮有上限的二次查询。

        为避免协作链无限循环，当前只取最多 3 个修正任务，并且只执行一轮。
        """
        suggestions = review.get("revision_suggestions", {})
        if not isinstance(suggestions, dict):
            return []

        valid_intents = set(self.intent_map.keys()) | {"attraction"}
        revision_tasks = []
        for intent, query in list(suggestions.items())[:3]:
            if intent in valid_intents:
                # 只接受当前系统能执行的 intent，避免 CriticAgent 要求不存在的工具。
                revision_tasks.append({
                    "intent": intent,
                    "query": query_to_text(query),
                })

        if not revision_tasks:
            # CriticAgent 认为无需修正，或者建议都不可执行时，协作链直接进入最终汇总。
            return []

        logger.info(f"根据 CriticAgent 建议执行 {len(revision_tasks)} 个修正任务")
        results = await asyncio.gather(
            *[
                self._execute_task(task, round_no=2)
                for task in revision_tasks
            ],
            return_exceptions=True,
        )
        normalized_results = []
        for result in results:
            if isinstance(result, Exception):
                normalized_results.append({
                    "round": 2,
                    "intent": "unknown",
                    "agent": "unknown",
                    "query": "",
                    "result": f"修正任务失败：{result}",
                })
            else:
                normalized_results.append(result)
        return normalized_results

    def _summarize(
        self,
        user_input: str,
        collaboration_plan: dict,
        agent_results: list,
        critic_review: dict,
    ) -> str:
        """调用 SummaryAgent 生成最终面向用户的旅行方案。"""
        if all_results_unavailable(agent_results):
            return (
                "当前方案无法生成，因为天气、票务、租车、保险等关键数据源都未返回有效结果。"
                "请先恢复数据库或在更接近出行日期时重新查询。"
            )

        chain = SmartVoyagePrompts.collaborative_summary_prompt() | self.llm
        return chain.invoke({
            "query": user_input,
            "current_date": current_date_str(),
            "collaboration_plan": json.dumps(
                collaboration_plan, ensure_ascii=False
            ),
            "agent_results": json.dumps(agent_results, ensure_ascii=False),
            "critic_review": json.dumps(critic_review, ensure_ascii=False),
        }).content.strip()

    async def plan(
        self,
        user_input: str,
        intents: list,
        user_queries: dict,
        run_meta: dict | None = None,
    ) -> str:
        """执行完整协作链并返回最终总结文本。"""
        # Flow step 11: 强协作入口。只有综合旅行目标会进入 Planner/Critic/Summary 链。
        collaboration_plan = self._build_plan(user_input, intents, user_queries)
        tasks = collaboration_plan.get("agent_tasks", [])
        logger.info(f"进入多智能体协作流程，任务数: {len(tasks)}")

        first_round = await asyncio.gather(
            *[self._execute_task(task) for task in tasks],
            return_exceptions=True,
        )
        agent_results = []
        for result in first_round:
            if isinstance(result, Exception):
                # gather(return_exceptions=True) 不让单个专家失败中断整条协作链，
                # 失败信息会进入 Critic/Summary，让最终答案解释缺失项。
                agent_results.append({
                    "round": 1,
                    "intent": "unknown",
                    "agent": "unknown",
                    "query": "",
                    "result": f"协作任务失败：{result}",
                })
            else:
                agent_results.append(result)

        critic_review = self._review(user_input, collaboration_plan, agent_results)
        revision_results = await self._run_revision_tasks(critic_review)
        if revision_results:
            # 修正结果追加到同一个 agent_results，SummaryAgent 能同时看到第一轮和第二轮。
            agent_results.extend(revision_results)
            critic_review = self._review(
                user_input, collaboration_plan, agent_results
            )

        summary = self._summarize(
            user_input, collaboration_plan, agent_results, critic_review
        )
        self.trace_recorder.record_collaboration(
            # 协作 trace 在这里一次性写入，因为此时 Planner、专家结果、Critic 和 Summary
            # 四个阶段都已经完成，前端可以展示完整链路。
            user_input,
            collaboration_plan,
            agent_results,
            critic_review,
            summary,
            run_meta=run_meta,
        )
        return summary
