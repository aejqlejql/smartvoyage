"""日期标准化和保守回复判断的共享工具。

本模块提供两类被多个链路复用的"守门"（guardrail）逻辑，目的是防止 LLM 在
数据缺失或数据源故障时凭空编造业务事实（价格、温度、余票等）：

1. **日期标准化**
   - 用户常说"5月1日""3号"这类没有年份的日期，下游工具需要绝对日期。
   - 这里把无年份的中文日期自动补全为 ``YYYY-MM-DD``，便于数据库/API 查询。

2. **无数据 / 故障信号识别**
   - A2A/MCP 返回的文本或 JSON 需要区分"查询无记录"（no_data）和
     "数据库/网络故障"（error），两者对用户的提示和后续处理完全不同。
   - 提供一组匹配函数，供 SummaryAgent 判断是否允许自由总结业务数据。

这些规则是"确定性"的（正则 + 字符串匹配），不依赖 LLM，因此可以写成纯函数
并被单元测试覆盖（见 tests/test_guardrails.py）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytz

# 统一使用上海（北京时间）时区，因为业务面向中文用户，日期判断必须按北京时间。
_SHANGHAI_TZ = pytz.timezone("Asia/Shanghai")

# 匹配"无年份的 X月Y日/号"的正则表达式。
# 说明：
#   (?<!\d)(?<!年)   —— 前面不能是数字或"年"，避免误匹配"2026年5月1日"里已有年份的写法。
#   (?P<month>...)   —— 月份，支持 1-12，允许 01-09 或 1-9 的写法。
#   (?P<day>...)     —— 日期，支持 1-31，同样允许前导零。
#   [日号]?          —— "日"或"号"结尾，可选。
# 例如："5月1日"、"3月2号"、"12月25" 都能匹配；"2026年5月1日"不匹配（前面有"年"）。
_YEARLESS_MONTH_DAY_RE = re.compile(
    r"(?<!\d)(?<!年)(?P<month>1[0-2]|0?[1-9])月(?P<day>3[01]|[12]\d|0?[1-9])[日号]?"
)

# "查询无记录"的信号标记。出现这些文本，说明查询本身是有效的，但数据库/API
# 里没有匹配数据——这属于正常业务结果，不是故障，不应重试。
_NO_DATA_MARKERS = (
    '"status": "no_data"',   # JSON 里的 status 字段（双引号形式）
    "'status': 'no_data'",   # JSON 里的 status 字段（单引号形式）
    "no_data",               # 兜底：裸字符串
    "未找到",                 # 工具返回的自然语言提示
    "暂无数据",
    "没有天气记录",
    "没有票务记录",
    "没有行程记录",
)

# "数据源故障"的标记。出现这些文本，说明数据库连接、网络或服务本身出了问题，
# 需要提示用户"稍后重试"，且绝不能据此编造数据。
_DATA_SOURCE_ERROR_MARKERS = (
    "database_unavailable",   # 工具返回的错误码
    "mysql connection not available",  # MySQL 不可用
    "数据库连接失败",
    "数据源暂时不可用",
    "系统连接异常",
    "系统连接问题",
    "服务暂时不可用",
    "连接异常",
    "无法获取",
    "无法查询",
    "查询失败",
    "处理失败",
    "暂时不可用",
)


def current_date_str() -> str:
    """返回当前北京时间，格式为 YYYY-MM-DD。

    用于注入到 intent/planning/summary 等 prompt，让 LLM 知道"今天"是哪一天，
    从而正确处理"明天""后天"等相对日期。
    """
    return datetime.now(_SHANGHAI_TZ).strftime("%Y-%m-%d")


def current_year() -> int:
    """返回当前北京时区的年份。

    用于给"无年份日期"补年份时的默认值（如"5月1日"默认补成今年）。
    """
    return datetime.now(_SHANGHAI_TZ).year


def normalize_yearless_dates(text: str, default_year: int | None = None) -> str:
    """把没有年份的中文月日日期补成绝对日期。

    例如：
        "北京5月1日的天气" -> "北京2026-05-01的天气"（假设 default_year=2026）

    参数：
        text: 待处理的文本。
        default_year: 补年份时使用的年份，缺省用当前年份。

    返回值：
        处理后的文本；如果传入的不是非空字符串，原样返回（不做处理）。
    """
    # 非字符串或空串直接返回，避免对 None 等类型调用正则出错。
    if not isinstance(text, str) or not text:
        return text

    # 未显式指定年份时，用当前年份。
    year = default_year or current_year()

    def repl(match: re.Match) -> str:
        """把匹配到的"X月Y日"替换成"YYYY-MM-DD"。"""
        month = int(match.group("month"))
        day = int(match.group("day"))
        # 补零到两位，统一输出 YYYY-MM-DD 格式。
        return f"{year:04d}-{month:02d}-{day:02d}"

    # 用 sub 替换所有匹配到的无年份日期。
    return _YEARLESS_MONTH_DAY_RE.sub(repl, text)


def normalize_query_payload(value: Any, default_year: int | None = None) -> Any:
    """递归标准化查询对象中的无年份日期。

    LLM 的查询输出可能是字符串、列表、字典的任意嵌套结构，
    本函数递归遍历并对其中的每个字符串调用 ``normalize_yearless_dates``。

    参数：
        value: 任意结构的数据（str/list/tuple/dict/其他）。
        default_year: 补年份时使用的年份。

    返回值：
        同构的新对象，其中所有字符串都已标准化日期。
    """
    if isinstance(value, str):
        return normalize_yearless_dates(value, default_year=default_year)
    if isinstance(value, list):
        return [normalize_query_payload(item, default_year=default_year) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_query_payload(item, default_year=default_year) for item in value)
    if isinstance(value, dict):
        return {
            key: normalize_query_payload(item, default_year=default_year)
            for key, item in value.items()
        }
    # 数字、布尔、None 等类型直接返回。
    return value


def contains_no_data_signal(text: Any) -> bool:
    """判断文本是否在表达无数据、不可用或需要补充参数。

    这是一个"宽口径"判断：只要出现无数据、数据源故障或需要补充参数三类信号
    中的任意一种，就返回 True。适用于需要保守兜底的场景（如 SummaryAgent）。

    注意：它内部同时包含了 contains_explicit_no_data_signal 和
    contains_data_source_error 的判断，是两者的并集。
    """
    if not isinstance(text, str) or not text.strip():
        return False

    lowered = text.lower()
    return (
        contains_explicit_no_data_signal(text)
        or contains_data_source_error(text)
        or any(marker in text or marker in lowered for marker in (
            "缺少必要信息",   # 工具提示参数不完整
            "请确认",         # 需要用户补充/确认
            "请提供",
            "请输入",
            "input_required", # ExpertResult 的 input_required 状态
        ))
    )


def contains_explicit_no_data_signal(text: Any) -> bool:
    """判断文本是否明确表示查询无匹配记录（正常业务结果）。

    与数据源故障不同，"无记录"意味着查询有效但结果为空，无需重试，
    提示用户调整查询条件即可。
    """
    if not isinstance(text, str) or not text.strip():
        return False
    lowered = text.lower()
    # 大小写不敏感匹配，兼容中英文标记。
    return any(marker in text or marker in lowered for marker in _NO_DATA_MARKERS)


def contains_data_source_error(text: Any) -> bool:
    """判断文本是否明确表示数据库、网络或数据源故障。

    与"无记录"不同，"故障"是可重试的（稍后再试可能恢复），
    但绝不能据此编造任何业务数据。
    """
    if not isinstance(text, str) or not text.strip():
        return False
    lowered = text.lower()
    return any(
        marker in text or marker in lowered
        for marker in _DATA_SOURCE_ERROR_MARKERS
    )


def all_results_unavailable(agent_results: Any) -> bool:
    """判断协作链里的专家结果是否全部不可用。

    协作链（CollaborationEngine）执行完多个专家 Agent 后，若所有结果都是
    无数据/故障/需补充，就没有可汇总的有效内容，此时应直接返回兜底提示，
    而不是让 SummaryAgent 凭空生成方案。

    参数：
        agent_results: 专家结果列表，每项可能是 dict（含 status 字段）或普通结果。

    返回值：
        - 空列表 → True（没有任何结果，视为全部不可用）。
        - 所有项都不可用 → True。
        - 存在至少一项可用 → False。
    """
    if not agent_results:
        return True

    saw_text = False  # 是否遇到过"有可判断内容"的项，避免全部为空时误判
    for item in agent_results:
        if not isinstance(item, dict):
            continue
        # 优先看结构化 status 字段（success/no_data/error/input_required）。
        status = str(item.get("status", "")).lower()
        if status:
            if status in {"no_data", "error", "input_required"}:
                saw_text = True
                continue
            # 出现 success 等可用状态，说明至少有一项可用。
            return False
        # 没有 status 字段时，退回用结果文本做信号判断。
        result_text = item.get("result", "")
        if result_text:
            saw_text = True
        if not contains_no_data_signal(result_text):
            # 结果文本里没有不可用信号，视为可用。
            return False
    return saw_text
