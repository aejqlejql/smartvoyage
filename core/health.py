"""SmartVoyage 部署依赖健康检查。

本模块提供一个统一的入口 ``collect_dependency_health``，用来检查 SmartVoyage
整套分布式服务里各个依赖是否可用。页面能打开只代表 FastAPI 进程活着，不代表
MySQL、MCP、A2A 这些下游服务都正常，所以单独抽出一层做探活。

检查范围：
    - MySQL：主服务记忆持久化和 MCP 数据查询共同依赖的关系数据库。
    - 天气/票务/行程 MCP：三个工具服务（Streamable HTTP 端口探活）。
    - 天气/票务/行程 A2A：三个专家服务（读取 Agent Card 判断是否在线）。
    - Milvus：可选的向量数据库，用于旅游团语义检索。

返回值结构：
    {
        "status": "ok" | "degraded",   # 所有必需依赖都 ok 才是 ok
        "dependencies": {
            "<name>": {"status": "ok"|"unavailable"|"optional_unavailable", "message": "...", ...}
        }
    }

注意：
    Milvus 属于可选依赖，即使不可用也只会让整体状态变成 degraded 或仍为 ok，
    不会影响 MySQL/MCP/A2A 这些必需依赖的判定。
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import mysql.connector
import requests

from ..infrastructure.config import Config


def collect_dependency_health(timeout: float = 2.0) -> dict:
    """检查 API 依赖服务状态，供部署和排障接口使用。

    参数：
        timeout: 每次探活的超时秒数，默认 2 秒，避免某个依赖挂起拖慢健康检查。

    返回值：
        dict: 形如 ``{"status": "ok"|"degraded", "dependencies": {...}}``。
    """
    # Flow step 12: 部署排障入口。页面能打开不代表 MySQL/MCP/A2A 都可用。
    conf = Config()

    # 依次探测所有依赖，每个依赖都是一个独立的探测函数。
    # MCP/A2A 地址优先从环境变量读取，默认值对应本地开发端口。
    dependencies = {
        "mysql": _check_mysql(conf, timeout),
        "weather_mcp": _check_tcp_url(os.getenv("WEATHER_MCP_URL", "http://127.0.0.1:8002"), timeout),
        "ticket_mcp": _check_tcp_url(os.getenv("TICKET_MCP_URL", "http://127.0.0.1:8001"), timeout),
        "trip_mcp": _check_tcp_url(os.getenv("TRIP_MCP_URL", "http://127.0.0.1:8003"), timeout),
        "weather_a2a": _check_agent_card(os.getenv("WEATHER_A2A_URL", "http://127.0.0.1:5005"), timeout),
        "ticket_a2a": _check_agent_card(os.getenv("TICKET_A2A_URL", "http://127.0.0.1:5006"), timeout),
        "trip_a2a": _check_agent_card(os.getenv("TRIP_A2A_URL", "http://127.0.0.1:5007"), timeout),
        "milvus": _check_milvus(conf, timeout),
    }

    # 必需依赖缺一不可：MySQL、三个 MCP、三个 A2A 都必须在线。
    # Milvus 是可选依赖，不纳入必需清单，因此不在下面列表里。
    required_names = [
        "mysql",
        "weather_mcp",
        "ticket_mcp",
        "trip_mcp",
        "weather_a2a",
        "ticket_a2a",
        "trip_a2a",
    ]

    # all() 判断所有必需依赖是否都返回 status == "ok"。
    required_ok = all(
        dependencies[name]["status"] == "ok"
        for name in required_names
    )

    return {
        "status": "ok" if required_ok else "degraded",
        "dependencies": dependencies,
    }


def _result(status: str, message: str = "", **extra) -> dict:
    """构造统一的依赖探测结果字典。

    参数：
        status: 状态值，如 "ok"、"unavailable"、"optional_unavailable"。
        message: 附加的说明文本，通常是异常信息或状态描述。
        **extra: 额外的键值对，如 ``url``、``name``，会合并进返回字典。

    返回值：
        dict: 至少包含 ``status`` 和 ``message`` 两个字段。
    """
    payload = {"status": status, "message": message}
    payload.update(extra)
    return payload


def _check_mysql(conf: Config, timeout: float) -> dict:
    """探测 MySQL 是否可连接。

    通过真实建立一次连接并立即关闭来判断数据库可用性。相比只 ping 端口，
    这种方式还能发现账号密码错误、数据库不存在等问题。
    """
    try:
        # connection_timeout 至少设为 1 秒，避免 timeout 传 0 导致无限等待。
        conn = mysql.connector.connect(
            host=conf.host,
            port=conf.port,
            user=conf.user,
            password=conf.password,
            database=conf.database,
            connection_timeout=max(1, int(timeout)),
        )
        conn.close()
        return _result("ok", url=f"{conf.host}:{conf.port}/{conf.database}")
    except Exception as exc:
        return _result("unavailable", str(exc), url=f"{conf.host}:{conf.port}/{conf.database}")


def _check_tcp_url(url: str, timeout: float) -> dict:
    """通过 TCP 连接探测一个 HTTP 服务端口是否监听。

    这里只做传输层探活（能建立 socket 连接即视为在线），不做 HTTP 语义校验，
    因为 MCP 服务的关键在于端口是否被 FastMCP 进程占用。
    """
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    # 没有显式端口时，根据协议猜测默认端口。
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return _result("ok", url=url)
    except Exception as exc:
        return _result("unavailable", str(exc), url=url)


def _check_agent_card(url: str, timeout: float) -> dict:
    """通过读取 Agent Card 探测一个 A2A 服务是否在线。

    A2A 协议规定每个 Agent 在 ``/.well-known/agent.json`` 暴露能力卡片，
    能成功读取卡片既说明服务在线，也能顺带拿到 Agent 名称。
    """
    card_url = f"{url.rstrip('/')}/.well-known/agent.json"
    try:
        response = requests.get(card_url, timeout=timeout)
        response.raise_for_status()
        card = response.json()
        return _result("ok", url=url, name=card.get("name", ""))
    except Exception as exc:
        return _result("unavailable", str(exc), url=url)


def _check_milvus(conf: Config, timeout: float) -> dict:
    """探测可选的 Milvus 向量数据库是否可用。

    Milvus 是可选依赖，即使失败也返回 ``optional_unavailable`` 而非
    ``unavailable``，从而不影响 ``collect_dependency_health`` 对必需依赖的判定。
    """
    try:
        # pymilvus 在项目里是可选安装，这里延迟导入，避免无向量功能时导入报错。
        from pymilvus import connections, utility

        # 使用独立 alias，避免和业务代码里的 default 连接互相干扰。
        alias = "health_check"
        connections.connect(
            alias=alias,
            host=conf.milvus_host,
            port=conf.milvus_port,
            timeout=timeout,
        )
        # 检查旅游团 collection 是否已初始化，能反映 RAG 功能是否就绪。
        healthy = utility.has_collection(conf.tour_group_collection, using=alias)
        connections.disconnect(alias=alias)
        return _result(
            "ok",
            "collection_exists" if healthy else "collection_not_initialized",
            url=f"{conf.milvus_host}:{conf.milvus_port}/{conf.tour_group_collection}",
        )
    except Exception as exc:
        return _result(
            "optional_unavailable",
            str(exc),
            url=f"{conf.milvus_host}:{conf.milvus_port}/{conf.tour_group_collection}",
        )
