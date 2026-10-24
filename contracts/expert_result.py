"""专家 Agent 返回结果的统一协议。

背景与目的：
    A2A/MCP 边界的专家 Agent 返回的是自然语言或 JSON 文本，格式不统一。
    如果让 SummaryAgent 直接读这些原始文本，它可能：
    - 在"无数据"（no_data）时继续编造价格/温度/余票等业务事实；
    - 在"数据源故障"（error）时也当作正常结果做总结。

    因此本模块定义 ``ExpertResult`` 这个统一结构，把各种文本收敛成四种
    明确的 ``status``，作为 SummaryAgent 的"可信输出边界"：

        - ``success``        查询成功，data 里有真实数据，可以正常总结。
        - ``no_data``        查询有效但无匹配记录，只能说明"暂无数据"。
        - ``error``          数据库/网络/服务故障，只能说明"数据源不可用"。
        - ``input_required`` 缺少必要参数，需要向用户追问补充。

    只有 status 为 success 时，SummaryAgent 才被允许引用具体业务数据。

提供的工厂方法：
    - ``success / no_data / error / input_required``：按状态显式构造结果。
    - ``from_text``：从 A2A artifact 或 MCP JSON 字符串自动解析出统一结构。

状态非法时会在 ``__post_init__`` 中自动纠正为 error，保证下游永远拿到合法状态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.guardrails import (
    contains_data_source_error,
    contains_explicit_no_data_signal,
)

# 合法的 status 集合，__post_init__ 会用它对非法值做兜底纠正。
VALID_STATUSES = {"success", "no_data", "error", "input_required"}


@dataclass
class ExpertResult:
    """跨 Agent 传递的结构化业务结果。"""

    # Flow step 10: 可信输出边界。SummaryAgent 只能在 success 数据上做总结。
    status: str       # 结果状态：success / no_data / error / input_required
    agent: str        # 产出该结果的 Agent 名称，如 "WeatherQueryAssistant"
    data: Any = None  # 业务数据（成功时为列表/字典；失败时为空）
    message: str = ""        # 附加说明文本（成功提示或失败原因）
    source: str = ""         # 结果来源，如 "a2a"、"mcp"、"collaboration"
    error_code: str = ""     # 错误码，如 "data_source_unavailable"
    raw: str = ""            # 原始返回文本，保留以便排查
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元信息

    def __post_init__(self):
        """构造后校验 status，非法值统一纠正为 error。

        这是 dataclass 的钩子，会在实例创建后自动执行，保证任何地方构造的
        ExpertResult 都持有合法状态，下游无需再担心脏数据。
        """
        if self.status not in VALID_STATUSES:
            self.status = "error"
            if not self.error_code:
                self.error_code = "invalid_status"

    @property
    def unavailable(self) -> bool:
        """是否属于不能进入自由总结的状态。

        只要不是 success，就认为是"不可用"，SummaryAgent 只能做保守说明，
        不能引用具体业务数据。
        """
        return self.status in {"no_data", "error", "input_required"}

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典，字段稳定供 Summary/Trace 使用。"""
        return {
            "status": self.status,
            "agent": self.agent,
            "data": [] if self.data is None else self.data,  # data 为 None 时统一成空列表
            "message": self.message,
            "source": self.source,
            "error_code": self.error_code,
            "raw": self.raw,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        """转换为 JSON 字符串，作为 A2A artifact 的文本载体。

        专家服务把结果包装成该 JSON 文本写进 task.artifacts，主服务读到后再
        用 from_text 解析回结构化对象。
        """
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def summary_input(self) -> str:
        """供总结 prompt 使用的受控文本。

        等价于 to_json；单独命名是为了在 ChatService 里表达"这是喂给总结 LLM 的
        受控输入"，语义更清晰。
        """
        return self.to_json()

    @classmethod
    def success(
        cls,
        agent: str,
        data: Any = None,
        message: str = "",
        source: str = "",
        raw: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "ExpertResult":
        """构造一个查询成功的结果（data 为真实业务数据）。"""
        return cls(
            status="success",
            agent=agent,
            data=data,
            message=message,
            source=source,
            raw=raw,
            metadata=metadata or {},
        )

    @classmethod
    def no_data(
        cls,
        agent: str,
        message: str,
        source: str = "",
        raw: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "ExpertResult":
        """构造一个"查询无记录"的结果（正常业务结果，非故障）。"""
        return cls(
            status="no_data",
            agent=agent,
            data=[],
            message=message,
            source=source,
            raw=raw,
            metadata=metadata or {},
        )

    @classmethod
    def error(
        cls,
        agent: str,
        message: str,
        error_code: str = "",
        source: str = "",
        raw: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "ExpertResult":
        """构造一个"数据源/服务故障"的结果。"""
        return cls(
            status="error",
            agent=agent,
            data=[],
            message=message,
            source=source,
            error_code=error_code,
            raw=raw,
            metadata=metadata or {},
        )

    @classmethod
    def input_required(
        cls,
        agent: str,
        message: str,
        raw: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "ExpertResult":
        """构造一个"缺少必要参数、需要追问"的结果。"""
        return cls(
            status="input_required",
            agent=agent,
            data=[],
            message=message,
            raw=raw,
            metadata=metadata or {},
        )

    @classmethod
    def from_text(cls, text: str, agent: str = "", source: str = "") -> "ExpertResult":
        """从 A2A artifact 或 MCP JSON 字符串解析统一结果。

        解析策略（按优先级）：
        1. 先尝试把文本解析成 JSON 对象（可能是标准 ExpertResult JSON，也可能是
           专家 Agent 自定义的 {status, data, message} 结构）。
        2. 解析成功后按 status 字段分发到对应的工厂方法。
        3. 解析失败或不是 JSON 时，退回用关键词做信号判断：
           - 无数据标记 → no_data
           - 数据源故障标记 → error
           - 需要补充参数 → input_required
           - 否则 → success（把原文当 message）

        参数：
            text: 原始返回文本。
            agent: 兜底 Agent 名（文本里没带 agent 字段时使用）。
            source: 结果来源。

        返回值：
            ExpertResult: 收敛后的统一结果。
        """
        raw = str(text or "").strip()
        # 先尝试解析 JSON；成功且是 dict 就按结构化字段分发。
        parsed = _loads_json(raw)
        if isinstance(parsed, dict):
            status = str(parsed.get("status", "success")).strip()
            message = str(parsed.get("message", "") or "")
            # agent/source 优先用 JSON 里的值，缺省时回退到入参。
            parsed_agent = str(parsed.get("agent", "") or agent)
            parsed_source = str(parsed.get("source", "") or source)
            error_code = str(parsed.get("error_code", "") or parsed.get("code", "") or "")
            data = parsed.get("data", [])
            metadata = parsed.get("metadata", {})
            if status == "error":
                return cls.error(
                    parsed_agent,
                    message or raw,
                    error_code=error_code,
                    source=parsed_source,
                    raw=raw,
                    metadata=metadata if isinstance(metadata, dict) else {},
                )
            if status == "no_data":
                return cls.no_data(
                    parsed_agent,
                    message or "暂无匹配数据。",
                    source=parsed_source,
                    raw=raw,
                    metadata=metadata if isinstance(metadata, dict) else {},
                )
            if status == "input_required":
                return cls.input_required(
                    parsed_agent,
                    message or "请补充必要信息。",
                    raw=raw,
                    metadata=metadata if isinstance(metadata, dict) else {},
                )
            return cls.success(
                parsed_agent,
                data=data,
                message=message,
                source=parsed_source,
                raw=raw,
                metadata=metadata if isinstance(metadata, dict) else {},
            )

        # 不是 JSON 对象时，退回关键词信号判断。
        if contains_explicit_no_data_signal(raw):
            return cls.no_data(agent, raw or "暂无匹配数据。", source=source, raw=raw)
        if contains_data_source_error(raw):
            return cls.error(
                agent,
                raw or "数据源暂时不可用。",
                error_code="data_source_unavailable",
                source=source,
                raw=raw,
            )
        if any(marker in raw for marker in ("请提供", "请输入", "缺少")):
            return cls.input_required(agent, raw or "请补充必要信息。", raw=raw)
        # 默认按成功处理，把原文放进 message。
        return cls.success(agent, message=raw, source=source, raw=raw)


def _loads_json(text: str) -> Any:
    """安全解析 JSON；失败时返回 None。

    兼容几种常见情况：
    - 纯 JSON 文本。
    - Markdown 代码块包裹的 JSON（去掉 ``` 围栏行）。
    - 前后带少量非 JSON 文本、中间是 JSON 对象的文本（截取首尾花括号）。

    任何解析失败都返回 None，由调用方决定后续兜底处理。
    """
    if not text:
        return None
    candidate = text.strip()
    # 去掉 Markdown 代码块围栏（```json ... ```）。
    if candidate.startswith("```"):
        lines = [
            line for line in candidate.splitlines()
            if not line.strip().startswith("```")
        ]
        candidate = "\n".join(lines).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # 尝试截取第一个 { 到最后一个 } 之间的内容再解析。
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None
