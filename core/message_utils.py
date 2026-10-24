"""LLM 输出和 Agent 查询文本的通用处理工具。

多个模块都需要做两件重复的小事：

1. 把 LLM 生成的结构化 query 转成 Agent 可接收的文本。
2. 把 LLM 返回的 JSON 文本解析成字典，同时兼容 ```json 代码块。

这些逻辑如果散落在 ChatService、CollaborationEngine、LangGraph 节点中，后续很容易
出现解析规则不一致。集中到本文件后，所有链路都使用同一套规则。
"""

import json
import re


def query_to_text(query) -> str:
    """把查询对象转成文本。

    LLM 有时会返回字符串，有时会返回 dict/list 等结构化对象。A2A Agent 当前接收
    文本消息，因此这里统一把非字符串对象 JSON 序列化，且保留中文字符。
    """
    if isinstance(query, str):
        return query
    return json.dumps(query, ensure_ascii=False)


def _extract_json_object(text: str) -> str:
    """去掉 Markdown 围栏，并尽量截出第一个 JSON 对象。"""
    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        text.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            cleaned = match.group(0)
    return cleaned


def _repair_llm_json(text: str) -> str:
    """修复大模型常见的伪 JSON：中文标点、智能引号、尾逗号、Python 字面量。"""
    repaired = text.translate(str.maketrans({
        "，": ",",
        "：": ":",
        "；": ",",
        "【": "[",
        "】": "]",
    }))
    repaired = (
        repaired.replace("“", '"')
        .replace("”", '"')
        .replace("„", '"')
        .replace("‟", '"')
    )
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r"\bTrue\b", "true", repaired)
    repaired = re.sub(r"\bFalse\b", "false", repaired)
    repaired = re.sub(r"\bNone\b", "null", repaired)
    return repaired


def parse_llm_json(text: str) -> dict:
    """解析 LLM 返回的 JSON 字符串。

    大模型常见输出形式有三种：

    - 纯 JSON：``{"intents": [...]}``
    - Markdown 代码块：```json ... ```
    - 前后带少量解释文本，但中间包含 JSON 对象

    该函数会尽量提取第一个 JSON 对象，再交给 ``json.loads`` 做严格解析。
    若因中文逗号/冒号等伪 JSON 失败，会先修复再解析。
    仍然不合法时异常向上抛出，由 LangGraph error 节点统一记录错误 trace。
    """
    if text is None or not str(text).strip():
        raise json.JSONDecodeError("empty llm json", text or "", 0)
    cleaned = _extract_json_object(str(text))
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = json.loads(_repair_llm_json(cleaned))
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("llm json is not an object", cleaned, 0)
    return parsed
