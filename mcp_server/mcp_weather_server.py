# ============================================================
# MCP 天气服务模块
# 功能：提供天气查询的 MCP（Model Context Protocol）工具接口
# 支持两种数据源：MySQL 数据库 / 和风天气 API（通过配置切换）
# 启动命令：python -m ai01_SmartVoyage.mcp_server.mcp_weather_server
# 访问地址：http://127.0.0.1:8002/mcp
#
# 调用流程图：
#
#   AI 智能体（如 Claude）
#        │
#        ▼
#   ┌─────────────────────────────┐
#   │  MCP Server (端口 8002)      │
#   │  create_weather_mcp_server() │
#   │  FastMCP("WeatherTools")     │
#   └──────────────┬──────────────┘
#                  │
#                  ▼
#           query_weather(city, start_date, end_date)
#                  │
#        ┌─────────┴─────────┐
#        │ 检查 weather_source│
#        └─────────┬─────────┘
#                  │
#        ┌─────────┴─────────┐
#        │                   │
#     "api"               其他（"db"）
#        │                   │
#        ▼                   ▼
#   ┌──────────────┐  ┌──────────────────┐
#   │ fetch_weather│  │ WeatherService   │
#   │ _from_api()  │  │ .query_weather() │
#   └──────┬───────┘  └────────┬─────────┘
#          │                   │
#          ▼                   ▼
#   ① geo接口:城市→ID     SQL查询 weather_data 表
#          │           SELECT * FROM weather_data
#          ▼            WHERE city = ? AND
#   ② 天气API:用ID获取数据    fx_date BETWEEN ? AND ?
#          │                   │
#          ▼                   ▼
#   ③ 过滤日期范围         遍历结果集（字典格式）
#   ④ 字段名映射           处理 date/Decimal 类型
#      tempMax→temp_max      │
#      textDay→text_day      ▼
#                     json.dumps() 序列化为 JSON 字符串
#          │                   │
#          └─────────┬─────────┘
#                    ▼
#           JSON 字符串返回给 AI 智能体
#
#   返回格式示例：
#   {
#     "status": "success",
#     "data": [
#       {
#         "city": "成都",
#         "fx_date": "2026-04-29",
#         "temp_max": 28,
#         "temp_min": 18,
#         "text_day": "多云",
#         "text_night": "晴",
#         "humidity": 65,
#         "wind_dir_day": "南风",
#         "wind_scale_day": "3",
#         "precip": 0.0
#       }
#     ]
#   }
#
# 数据源切换：修改 infrastructure/config.py 中的 weather_source 配置
#   weather_source = "api"  → 使用和风天气实时 API
#   weather_source = "db"   → 使用本地 MySQL 数据库
# ============================================================

import json  # JSON 序列化/反序列化，将 Python 字典转为 JSON 字符串返回
import os
from datetime import date, datetime, timedelta  # 日期类型，处理数据库返回的时间字段
from decimal import Decimal  # 高精度数字类型，处理数据库返回的数值字段

import mysql.connector  # MySQL 数据库驱动，用于连接和查询本地天气数据
import requests  # HTTP 请求库，用于调用和风天气 API
from mcp.server.fastmcp import FastMCP

from ..infrastructure.config import Config
from ..infrastructure.logging import logger
from ..utils.format import DateEncoder, default_encoder

# 加载全局配置对象
conf = Config()


class WeatherService:
    """
    天气服务类：负责从 MySQL 数据库查询天气数据
    工作原理：在初始化时建立数据库长连接，后续查询复用该连接
    """
    def __init__(self):
        """建立天气数据库连接。

        数据库模式下，天气 MCP 工具会查询本地 weather_data 表；连接在服务启动时
        建立，可以尽早暴露密码、库名或端口配置错误。API 模式会在服务工厂中跳过
        WeatherService 实例化，因此不会建立该连接。
        """
        self.conn = self._connect()

    @staticmethod
    def _connection_kwargs() -> dict:
        """返回完整数据库连接参数，包含 .env 配置的端口。"""
        return {
            "host": conf.host,
            "port": conf.port,
            "user": conf.user,
            "password": conf.password,
            "database": conf.database,
            "connection_timeout": conf.db_connect_timeout,
        }

    def _connect(self):
        """创建新的 MySQL 连接。"""
        return mysql.connector.connect(**self._connection_kwargs())

    def _ensure_connection(self):
        """在每次查询前验证连接，数据库重启或超时后自动重连。"""
        try:
            self.conn.ping(reconnect=True, attempts=1, delay=0)
        except Exception:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = self._connect()

    def query_weather(self, city: str, start_date: str, end_date: str) -> str:
        """
        参数化查询数据库天气数据，返回 JSON 字符串

        参数：
            city: 城市名称，如 "成都"
            start_date: 开始日期，格式 "YYYY-MM-DD"
            end_date: 结束日期，格式 "YYYY-MM-DD"

        返回：
            JSON 字符串，包含 status（状态）和 data（天气数据列表）
        """
        try:
            self._ensure_connection()
            # 创建游标，dictionary=True 使返回结果为字典而非元组
            cursor = self.conn.cursor(dictionary=True)

            # 参数化 SQL 查询：使用 %s 占位符防止 SQL 注入攻击
            # 查询 weather_data 表中指定城市和时间范围内的天气记录
            sql = ("SELECT city, fx_date, temp_max, temp_min, text_day, text_night, "
                   "humidity, wind_dir_day, wind_scale_day, precip FROM weather_data "
                   "WHERE city = %s AND fx_date BETWEEN %s AND %s ORDER BY fx_date")
            # 执行查询，参数以列表形式传入，自动转义防止注入
            cursor.execute(sql, [city, start_date, end_date])

            # 获取所有匹配的查询结果
            results = cursor.fetchall()
            cursor.close()  # 关闭游标释放资源

            # 处理特殊类型字段：date/Decimal 等类型无法直接 JSON 序列化，需要转换
            for result in results:
                for key, value in result.items():
                    if isinstance(value, (date, datetime, timedelta, Decimal)):
                        result[key] = default_encoder(value)  # 转为字符串格式

            # 根据是否有结果返回不同格式：
            # - 有数据：status="success" + data 列表
            # - 无数据：status="no_data" + 提示信息
            # ensure_ascii=False 保证中文正常显示而非 Unicode 转义
            return json.dumps(
                {"status": "success", "data": results} if results
                else {"status": "no_data", "message": "未找到天气数据，请确认城市和日期。"},
                cls=DateEncoder, ensure_ascii=False
            )
        except Exception as e:
            # 捕获异常，记录日志并返回错误信息
            logger.error(f"天气查询错误: {str(e)}")
            return json.dumps({
                "status": "error",
                "code": "database_unavailable",
                "message": "天气数据源暂时不可用：数据库连接失败。",
            }, ensure_ascii=False)


def fetch_weather_from_api(city: str, start_date: str, end_date: str) -> str:
    """
    从和风天气 API 获取天气数据

    工作流程：
        1. 先用城市名调用 geo 接口获取 location_id
        2. 再用 location_id 调用天气预报接口获取数据
        3. 过滤出指定日期范围内的数据，格式化后返回

    参数：
        city: 城市名称，如 "成都"
        start_date: 开始日期，格式 "YYYY-MM-DD"
        end_date: 结束日期，格式 "YYYY-MM-DD"

    返回：
        JSON 字符串，格式与数据库查询一致
    """
    logger.info(f"从和风天气API获取数据: {city}, {start_date} ~ {end_date}")

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return json.dumps(
            {"status": "error", "message": "日期必须使用 YYYY-MM-DD 格式。"},
            ensure_ascii=False,
        )
    if start > end:
        return json.dumps(
            {"status": "error", "message": "开始日期不能晚于结束日期。"},
            ensure_ascii=False,
        )

    if not conf.weather_api_key:
        return json.dumps(
            {"status": "error", "message": "未配置 QWEATHER_API_KEY，无法调用天气 API。"},
            ensure_ascii=False,
        )

    # 设置请求头，传入和风天气 API Key（认证凭证）
    headers = {
        "X-QW-Api-Key": conf.weather_api_key
    }

    # 第一步：城市名 → 地理位置 ID（和风天气需要先查城市 ID）
    geo_url = f"https://{conf.weather_api_host}/geo/v2/city/lookup"
    geo_resp = requests.get(
        geo_url,
        headers=headers,
        params={"location": city},
        timeout=15,
    )
    geo_resp.raise_for_status()
    geo_data = geo_resp.json()

    if geo_data.get("code") != "200":
        return json.dumps(
            {"status": "error", "message": f"城市查询失败：{geo_data.get('code', 'unknown')}"},
            ensure_ascii=False,
        )

    # 如果 API 没有找到该城市，直接返回错误信息
    if not geo_data.get("location"):
        return json.dumps({"status": "no_data", "message": f"未找到城市：{city}"}, ensure_ascii=False)

    # 取第一个匹配的城市 ID
    location_id = geo_data["location"][0]["id"]

    # 第二步：用 location_id 获取天气预报数据
    weather_resp = requests.get(
        conf.weather_base_url,
        headers=headers,
        params={"location": location_id},
        timeout=15,
    )
    weather_resp.raise_for_status()
    weather_data = weather_resp.json()

    if weather_data.get("code") != "200":
        return json.dumps(
            {"status": "error", "message": f"天气查询失败：{weather_data.get('code', 'unknown')}"},
            ensure_ascii=False,
        )

    # 第三步：过滤日期范围，并将 API 返回的字段映射为数据库同名字段
    results = []
    for day in weather_data.get("daily", []):
        # 字符串比较即可，因为日期格式都是 "YYYY-MM-DD"
        if start_date <= day.get("fxDate", "") <= end_date:
            results.append({
                "city": city,                              # 城市名
                "fx_date": day.get("fxDate"),              # 预报日期
                "temp_max": int(day.get("tempMax", 0)),    # 最高温度（转为整数）
                "temp_min": int(day.get("tempMin", 0)),    # 最低温度（转为整数）
                "text_day": day.get("textDay", ""),        # 白天天气描述
                "text_night": day.get("textNight", ""),    # 夜间天气描述
                "humidity": int(day.get("humidity", 0)),   # 湿度百分比
                "wind_dir_day": day.get("windDirDay", ""), # 白天风向
                "wind_scale_day": day.get("windScaleDay", ""),  # 白天风力等级
                "precip": float(day.get("precip", 0))      # 降水量（保留浮点）
            })

    # 根据是否有结果返回对应格式
    if results:
        return json.dumps({"status": "success", "data": results}, ensure_ascii=False)
    return json.dumps({"status": "no_data", "message": "未找到天气数据。"}, ensure_ascii=False)

def create_weather_mcp_server():
    """
    创建并启动天气 MCP 服务器

    MCP（Model Context Protocol）：一种协议，允许 AI 模型（如 Claude）通过标准接口调用外部工具
    本模块将天气查询功能封装为 MCP 工具，供 AI 智能体调用
    """
    # Flow step 8: MCP 工具层。这里是真正执行天气数据查询的服务边界。
    # 创建 FastMCP 实例，相当于定义一个工具服务。
    bind_host = os.getenv("WEATHER_MCP_BIND_HOST", "127.0.0.1")
    port = int(os.getenv("WEATHER_MCP_PORT", "8002"))
    weather_mcp = FastMCP(
        name="WeatherTools",
        host=bind_host,
        port=port,
        streamable_http_path="/mcp",
    )

    # API 数据源不依赖 MySQL，只有数据库模式才建立连接。
    service = WeatherService() if conf.weather_source != "api" else None

    # 使用 @tool 装饰器注册 MCP 工具，description 明确约束日期格式。
    # name 是工具唯一标识，description 用于辅助上层 Agent 判断调用时机。
    @weather_mcp.tool(
        name="query_weather",
        description="查询天气数据，参数：city(城市), start_date(开始日期YYYY-MM-DD), end_date(结束日期YYYY-MM-DD)"
    )
    def query_weather(city: str, start_date: str, end_date: str) -> str:
        """
        MCP 工具入口函数：根据配置选择从数据库或 API 获取天气数据

        参数：
            city: 城市名称，如 "成都"
            start_date: 开始日期，格式 "YYYY-MM-DD"
            end_date: 结束日期，格式 "YYYY-MM-DD"

        返回：
            JSON 字符串，包含天气数据
        """
        logger.info(f"执行天气查询: {city}, {start_date} ~ {end_date}")

        # 根据 infrastructure/config.py 中的 weather_source 配置决定数据源：
        # - "api": 调用和风天气实时 API
        # - 其他值（如 "db"）: 查询本地 MySQL 数据库
        if conf.weather_source == "api":
            return fetch_weather_from_api(city, start_date, end_date)
        return service.query_weather(city, start_date, end_date)

    # 打印服务启动信息到日志
    logger.info("=== 天气MCP服务器信息 ===")
    logger.info(f"名称: {weather_mcp.name}")

    # 使用官方 Streamable HTTP 传输启动 MCP 服务。
    try:
        # 提示用户访问地址
        print(f"服务器已启动，请访问 http://{bind_host}:{port}/mcp")
        weather_mcp.run(transport="streamable-http")
    except Exception:
        logger.exception("天气 MCP 服务器启动失败")
        raise

if __name__ == '__main__':
    # 调试用法：直接调用 API 测试，不启动 MCP 服务
    # api = fetch_weather_from_api(city="成都", start_date="2026-04-28", end_date="2026-05-01")
    # print(api)

    # 启动 MCP 天气服务器
    create_weather_mcp_server()
