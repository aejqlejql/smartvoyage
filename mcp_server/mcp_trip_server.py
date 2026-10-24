"""
行程 MCP Server 模块
====================
功能：为智能旅游助手提供行程相关的 MCP 工具（Tool）
  - 租车查询 / 预订
  - 旅游团语义检索（Milvus RAG）/ 报名
  - 旅行保险查询 / 购买

数据源架构：
  - 租车、保险：MySQL 数据库（结构化查询）
  - 旅游团：Milvus 向量数据库（语义检索 RAG，通过 Qwen Embedding API 生成向量）

技术要点：
  1. 混合数据源：MySQL 处理结构化数据，Milvus 处理语义搜索
  2. 参数化查询防止 SQL 注入
  3. 通过 FastMCP 框架注册为 MCP 工具，供上层 A2A Agent 调用
"""

import json  # JSON 处理
import os
from datetime import date, datetime, timedelta  # 时间处理
from decimal import Decimal  # 高精度小数类型

import mysql.connector  # MySQL 数据库驱动
from mcp.server.fastmcp import FastMCP

from ..infrastructure.config import Config
from ..infrastructure.logging import logger
from ..utils.format import DateEncoder, default_encoder

conf = Config()


def get_embedding(text: str) -> list:
    """调用 Qwen Embedding API 生成旅游团语义检索向量。

    用途：
        把用户的自然语言查询（或旅游团描述）转成固定维度的向量，
        再交给 Milvus 做余弦相似度检索。

    参数：
        text: 需要向量化的文本，如"想看雪山的地方"。

    返回值：
        list: 浮点数向量，维度等于 conf.embedding_dim（默认 1024）。

    异常：
        - 未配置 API Key 时抛 RuntimeError。
        - 接口调用失败时由 raise_for_status 抛 HTTPError。
    """
    if not conf.api_key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法生成旅游团查询向量。")
    import requests  # 延迟导入，避免非向量场景下强依赖 requests
    headers = {
        "Authorization": f"Bearer {conf.api_key}",  # DashScope 用 Bearer Token 认证
        "Content-Type": "application/json"
    }
    # 向量维度必须与 Milvus collection 的 embedding 字段保持一致。
    payload = {
        "model": "text-embedding-v3",   # Qwen 的 embedding 模型
        "input": [text],                # 批量接口，这里一次只传一条
        "dimensions": conf.embedding_dim  # 指定输出维度
    }
    response = requests.post(conf.embedding_url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()  # 非 2xx 抛出异常，快速暴露配置/网络问题
    # 返回第一条输入的 embedding（因为我们只传了一条）。
    return response.json()["data"][0]["embedding"]


def search_tour_groups_in_milvus(query_text: str, city: str = None, limit: int = 5) -> list:
    """
    在 Milvus 中进行语义搜索，找到最匹配的旅游团。

    工作流程：
        1. 连接 Milvus 并加载旅游团 Collection
        2. 用 Qwen Embedding API 将用户查询文本转为向量
        3. 在 Milvus 中执行 COSINE 相似度检索（ANN 近似最近邻搜索）
        4. 可选按城市过滤（Milvus expr 表达式）

    参数：
        query_text (str): 用户自然语言查询，如"想看雪山的地方"
        city (str, optional): 城市过滤条件，如"成都"
        limit (int): 返回结果数量上限，默认 5

    返回值：
        list: 匹配的旅游团列表，每项包含 tour_id, tour_name, city, days, price, similarity 等
    """
    # Milvus 负责向量相似度检索，MySQL 只承载租车和保险等结构化数据。
    from pymilvus import Collection, connections  # 延迟导入，避免非向量场景强依赖 pymilvus
    connections.connect(alias="default", host=conf.milvus_host, port=conf.milvus_port)
    collection = Collection(conf.tour_group_collection)  # 拿到旅游团集合对象
    collection.load()  # 把集合加载进内存，才能执行检索

    # COSINE 相似度检索：向量夹角越小，语义越接近
    # nprobe=10：搜索时扫描 10 个聚类单元，平衡精度与速度
    results = collection.search(
        data=[get_embedding(query_text)],  # 把查询文本转成向量再检索
        anns_field="embedding",            # 在 embedding 向量字段上做近似最近邻搜索
        param={"metric_type": "COSINE", "params": {"nprobe": 10}},
        limit=limit,                        # 最多返回 limit 条
        output_fields=["tour_id", "tour_name", "city", "days", "price",
                       "total_seats", "remaining_seats", "agency", "rating",
                       "departure_dates", "highlights"],  # 需要回传的标量字段
        expr=f'city == "{city}"' if city else None  # 城市过滤：Milvus 的标量过滤表达式
    )

    tour_list = []
    # search 返回的是"每行查询的结果列表"，这里只有一行查询，所以外层循环一次。
    for hits in results:
        for hit in hits:
            tour_list.append({
                "tour_id": hit.entity.get("tour_id"),
                "tour_name": hit.entity.get("tour_name"),
                "city": hit.entity.get("city"),
                "days": hit.entity.get("days"),
                "price": hit.entity.get("price"),
                # distance 在 COSINE 度量下就是余弦相似度，值越大越相关
                "similarity": round(hit.distance, 4),
            })
    return tour_list


# ==================== 行程服务类 ====================
class TripService:
    """
    行程查询与预订服务类

    混合数据源架构：
    - 租车、保险：从 MySQL 数据库真实查询
    - 旅游团：从 Milvus 向量数据库语义检索（RAG）
    """

    def __init__(self):
        """建立 TripTools 需要的数据库连接。

        租车和保险查询依赖 MySQL；旅游团语义搜索依赖 Milvus，但 Milvus 在实际
        查询函数中连接。这里如果 MySQL 不可用就让 MCP 服务启动失败，避免对上游
        暴露半可用工具。
        """
        # 建立 MySQL 数据库连接
        # 数据库不可用时让 MCP 进程启动失败，避免对外暴露半可用服务。
        self.conn = self._connect()

    @staticmethod
    def _connection_kwargs() -> dict:
        """集中维护数据库连接参数。"""
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
        """每次查询前检查连接，数据库重启后自动重连。"""
        try:
            self.conn.ping(reconnect=True, attempts=1, delay=0)
        except Exception:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = self._connect()

    def _execute_query(self, sql: str, params: list = None) -> str:
        """
        执行 SQL 查询，返回 JSON 格式的结果

        参数：
            sql (str): SQL 查询语句，使用 %s 占位符
            params (list): SQL 参数列表

        返回值：
            str: JSON 字符串
        """
        try:
            self._ensure_connection()
            cursor = self.conn.cursor(dictionary=True)
            cursor.execute(sql, params or [])
            results = cursor.fetchall()
            cursor.close()

            # 处理特殊类型（日期、Decimal 等）
            for result in results:
                for key, value in result.items():
                    if isinstance(value, (date, datetime, timedelta, Decimal)):
                        result[key] = default_encoder(value)

            if results:
                response = {"status": "success", "data": results}
            else:
                response = {"status": "no_data", "message": "未找到行程数据，请确认查询条件。"}

            return json.dumps(response, cls=DateEncoder, ensure_ascii=False)

        except Exception as e:
            logger.error(f"行程查询错误: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

    # ========== 租车查询（MySQL） ==========

    def query_car_rental(self, pickup_city: str, return_city: str, date: str, car_type: str = None) -> str:
        """
        查询租车信息（从 MySQL car_rentals 表查询）
        """
        logger.info(f"查询租车: {pickup_city} -> {return_city}, {date}, {car_type}")
        sql = ("SELECT id, company, pickup_city, return_city, pickup_date, car_type, "
               "car_model, price_per_day, total_available, transmission, seats, deposit "
               "FROM car_rentals "
               "WHERE pickup_city = %s AND return_city = %s AND pickup_date = %s")
        params = [pickup_city, return_city, date]
        if car_type:
            sql += " AND car_type = %s"
            params.append(car_type)
        return self._execute_query(sql, params)

    # ========== 旅游团查询（Milvus RAG） ==========

    def query_tour_group(self, query_text: str, city: str = None) -> str:
        """
        查询旅游团信息（从 Milvus 向量数据库进行语义检索）

        参数：
            query_text (str): 用户查询描述，如"想看雪山的地方"、"适合亲子游的短途旅行"
            city (str, optional): 城市过滤条件

        返回值：
            str: JSON 字符串，包含匹配的旅游团列表
        """
        logger.info(f"查询旅游团: query='{query_text}', city='{city}'")
        try:
            tour_list = search_tour_groups_in_milvus(query_text, city, limit=5)

            if tour_list:
                return json.dumps({"status": "success", "data": tour_list}, ensure_ascii=False)
            else:
                return json.dumps({
                    "status": "no_data",
                    "message": f"未找到匹配的旅游团。{'请确认城市名称，' if city else ''}或尝试其他搜索条件。"
                }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"旅游团 RAG 查询错误: {str(e)}")
            return json.dumps({"status": "error", "message": f"旅游团查询出错：{str(e)}"}, ensure_ascii=False)

    # ========== 保险查询（MySQL） ==========

    def query_insurance(self, insurance_type: str = None) -> str:
        """
        查询旅行保险产品信息（从 MySQL insurances 表查询）
        """
        logger.info(f"查询保险: {insurance_type}")
        sql = ("SELECT id, insurance_type, name, company, coverage, price, "
               "duration_days, max_coverage, medical_coverage, baggage_coverage, flight_delay "
               "FROM insurances")
        params = []
        if insurance_type:
            sql += " WHERE insurance_type = %s"
            params.append(insurance_type)
        return self._execute_query(sql, params)

    # ========== 预订方法 ==========
    # 说明：以下三个预订方法均为演示实现，只回显参数并写日志，不真实落库。
    # 真实业务接入时应增加库存校验、事务、幂等控制（防止重复下单）。

    def order_car_rental(self, date: str, car_type: str, number: int) -> str:
        """预订租车。

        参数：
            date:     租车日期，格式 "YYYY-MM-DD"。
            car_type: 车型，如 "经济型"/"SUV"/"豪华型"/"MPV"。
            number:   租车数量。

        返回值：
            str: 预订成功提示文本（演示实现，未实际写库）。
        """
        logger.info(f"预订租车: {date}, {car_type}, {number}辆")
        return json.dumps({"status": "input_required", "data": [], "message": "租车预订暂未纳入订单范围，本工具未创建任何订单。"}, ensure_ascii=False)

    def order_tour_group(self, date: str, tour_name: str, number: int) -> str:
        """报名旅游团。

        参数：
            date:      出发日期，格式 "YYYY-MM-DD"。
            tour_name: 旅游团名称（需与查询结果中的团名一致）。
            number:    报名人数。

        返回值：
            str: 报名成功提示文本（演示实现，未实际写库）。
        """
        logger.info(f"报名旅游团: {date}, {tour_name}, {number}人")
        return json.dumps({"status": "input_required", "data": [], "message": "旅游团预订暂未纳入订单范围，本工具未创建任何订单。"}, ensure_ascii=False)

    def order_insurance(self, insurance_type: str, date: str, number: int) -> str:
        """购买旅行保险。

        参数：
            insurance_type: 保险类型，如 "综合型"/"意外型"/"医疗型"/"境外型"。
            date:           生效日期，格式 "YYYY-MM-DD"。
            number:         购买份数。

        返回值：
            str: 购买成功提示文本（演示实现，未实际写库）。
        """
        logger.info(f"购买保险: {insurance_type}, {date}, {number}份")
        return json.dumps({"status": "input_required", "data": [], "message": "聊天不执行投保。请在行程方案页确认模拟保障；本工具未创建订单或保险合同。"}, ensure_ascii=False)


 # ========== 注册查询工具 ==========
def create_trip_mcp_server():
    """注册行程工具，并通过官方 Streamable HTTP 启动 MCP 服务。"""
    # Flow step 8: MCP 工具层。租车、保险走 MySQL，旅游团查询可选走 Milvus。
    # 行程工具同时包含 MySQL 查询、Milvus 检索和演示预订接口。
    bind_host = os.getenv("TRIP_MCP_BIND_HOST", "127.0.0.1")
    port = int(os.getenv("TRIP_MCP_PORT", "8003"))
    trip_mcp = FastMCP(
        name="TripTools",
        host=bind_host,
        port=port,
        streamable_http_path="/mcp",
    )

    # 所有工具共享一个服务对象和数据库连接。
    service = TripService()

    # 结构化查询工具
    @trip_mcp.tool(
        name="query_car_rental",
        description="查询租车信息，参数：pickup_city(取车城市), return_city(还车城市), date(日期，格式YYYY-MM-DD), car_type(车型类型，可选：经济型/SUV/豪华型/MPV)"
    )
    def query_car_rental(pickup_city: str, return_city: str, date: str, car_type: str = None) -> str:
        """MCP 工具入口：查询取车/还车城市、日期和车型对应的租车库存。"""
        return service.query_car_rental(pickup_city, return_city, date, car_type)

    @trip_mcp.tool(
        name="query_tour_group",
        description="查询旅游团信息（语义搜索），参数：query_text(查询描述，如'想看雪山的地方')，city(城市过滤，可选)"
    )
    def query_tour_group(query_text: str, city: str = None) -> str:
        """MCP 工具入口：通过 Milvus RAG 语义检索旅游团产品。"""
        return service.query_tour_group(query_text, city)

    @trip_mcp.tool(
        name="query_insurance",
        description="查询旅行保险产品，参数：insurance_type(保险类型，可选：综合型/意外型/医疗型/境外型)"
    )
    def query_insurance(insurance_type: str = None) -> str:
        """MCP 工具入口：查询旅行保险产品，可按保险类型过滤。"""
        return service.query_insurance(insurance_type)

    # 预订工具目前返回演示结果，真实业务接入时应增加事务和幂等控制。

    @trip_mcp.tool(
        name="order_car_rental",
        description="根据日期、车型和数量预订租车服务。参数：date(租车日期，格式YYYY-MM-DD), car_type(车型，如经济型/SUV/豪华型/MPV), number(租车数量)"
    )
    def order_car_rental(date: str, car_type: str, number: int) -> str:
        """MCP 工具入口：演示租车预订流程。"""
        logger.info(f"正在预订租车: {date}, {car_type}, {number}辆")
        return service.order_car_rental(date, car_type, number)

    @trip_mcp.tool(
        name="order_tour_group",
        description="根据出发日期、旅游团名称和人数报名旅游团。参数：date(出发日期，格式YYYY-MM-DD), tour_name(旅游团名称，需与查询结果中的团名一致), number(报名人数)"
    )
    def order_tour_group(date: str, tour_name: str, number: int) -> str:
        """MCP 工具入口：演示旅游团报名流程。"""
        logger.info(f"正在报名旅游团: {date}, {tour_name}, {number}人")
        return service.order_tour_group(date, tour_name, number)

    @trip_mcp.tool(
        name="order_insurance",
        description="根据保险类型、生效日期和份数购买旅行保险。参数：insurance_type(保险类型，如综合型/意外型/医疗型/境外型), date(生效日期，格式YYYY-MM-DD), number(购买份数)"
    )
    def order_insurance(insurance_type: str, date: str, number: int) -> str:
        """MCP 工具入口：演示旅行保险购买流程。"""
        logger.info(f"正在购买保险: {insurance_type}, {date}, {number}份")
        return service.order_insurance(insurance_type, date, number)

    print(f"服务器已启动，请访问 http://{bind_host}:{port}/mcp")
    # 由官方 SDK 负责会话、JSON-RPC 和 SSE/HTTP 细节。
    trip_mcp.run(transport="streamable-http")


if __name__ == '__main__':
    create_trip_mcp_server()
