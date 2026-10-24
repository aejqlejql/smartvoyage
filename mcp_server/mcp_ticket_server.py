"""
票务 MCP Server 模块
====================
功能：为智能旅游助手提供票务相关的 MCP 工具（Tool）
  - 火车票查询 / 预定
  - 机票查询 / 预定
  - 演唱会票查询 / 预定

技术要点：
  1. 使用 MySQL 参数化查询（%s 占位符），防止 SQL 注入
  2. 通过 FastMCP 框架将 Python 方法注册为 MCP 工具，供 Agent 调用
  3. 自定义 JSON 编码器处理 date/datetime/Decimal 等特殊类型
"""

import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal

import mysql.connector
from mcp.server.fastmcp import FastMCP

from ..infrastructure.config import Config
from ..infrastructure.logging import logger
from ..utils.format import DateEncoder, default_encoder

# 加载配置文件中的数据库连接参数
conf = Config()


# ============================================================
# 票务服务类 —— 封装所有票务相关的数据库查询和预定逻辑
# ============================================================
class TicketService:
    """
    票务服务类，负责火车票、机票、演唱会票的查询与预定。
    所有查询方法统一通过 _execute_query 执行参数化 SQL，返回标准 JSON 字符串。
    """

    def __init__(self):
        """初始化方法：建立 MySQL 数据库连接"""
        # MCP 服务启动时建立连接；连接失败直接让进程退出，便于编排系统重启。
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
        """每次查询前确认连接有效，必要时自动重连。"""
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
        执行参数化查询的通用方法。

        参数:
            sql:    SQL 语句，使用 %s 作为参数占位符（注意不是 Python 的 % 格式化）
            params: 参数列表，按顺序填充 %s 占位符

        返回:
            JSON 字符串，包含三种可能的结构：
              - {"status": "success", "data": [...]}       查询到数据
              - {"status": "no_data", "message": "..."}    无匹配数据
              - {"status": "error", "message": "..."}      SQL 执行异常

        关键知识点：
          - dictionary=True：cursor 返回字典而非元组，便于通过列名访问
          - default_encoder：将 date/datetime/Decimal 转为可序列化的字符串
          - DateEncoder：json.dumps 的自定义编码器类
        """
        try:
            self._ensure_connection()
            # 所有查询都通过参数绑定执行，禁止把用户输入拼接进 SQL。
            # 创建游标，dictionary=True 使结果以字典列表形式返回
            cursor = self.conn.cursor(dictionary=True)
            # 执行参数化查询 —— params 中的值会自动转义，防止 SQL 注入
            cursor.execute(sql, params or [])
            results = cursor.fetchall()
            cursor.close()

            # 遍历结果，将 date/datetime/Decimal 类型转换为字符串
            for result in results:
                for key, value in result.items():
                    if isinstance(value, (date, datetime, timedelta, Decimal)):
                        result[key] = default_encoder(value)

            # 根据是否有数据返回不同的 JSON 结构
            return json.dumps(
                {"status": "success", "data": results} if results
                else {"status": "no_data", "message": "未找到票务数据，请确认查询条件。"},
                cls=DateEncoder, ensure_ascii=False  # ensure_ascii=False 保证中文正常显示
            )
        except Exception as e:
            # 记录错误日志并返回错误 JSON
            logger.error(f"票务查询错误: {str(e)}")
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

    def query_train(self, departure_city: str, arrival_city: str, date: str, seat_type: str = None) -> str:
        """
        查询火车票。

        参数:
            departure_city: 出发城市，如 "北京"
            arrival_city:   到达城市，如 "上海"
            date:           出发日期，格式 "YYYY-MM-DD"
            seat_type:      座位类型（可选），如 "一等座"、"二等座"

        返回:
            JSON 字符串，包含车次、时间、价格、余票等信息

        SQL 说明：
          - WHERE 条件使用 %s 占位符，参数通过 params 列表传入
          - 如果指定了 seat_type，动态追加 AND 条件
          - DATE(departure_time) 提取日期部分进行匹配
        """
        sql = ("SELECT id, departure_city, arrival_city, departure_time, arrival_time, "
               "train_number, seat_type, price, remaining_seats FROM train_tickets "
               "WHERE departure_city = %s AND arrival_city = %s AND DATE(departure_time) = %s")
        params = [departure_city, arrival_city, date]
        if seat_type:
            sql += " AND seat_type = %s"
            params.append(seat_type)
        return self._execute_query(sql, params)

    def query_flight(self, departure_city: str, arrival_city: str, date: str, cabin_type: str = None) -> str:
        """
        查询机票。

        参数:
            departure_city: 出发城市
            arrival_city:   到达城市
            date:           出发日期，格式 "YYYY-MM-DD"
            cabin_type:     舱位类型（可选），如 "经济舱"、"商务舱"

        返回:
            JSON 字符串，包含航班号、时间、价格、余票等信息
        """
        sql = ("SELECT id, departure_city, arrival_city, departure_time, arrival_time, "
               "flight_number, cabin_type, price, remaining_seats FROM flight_tickets "
               "WHERE departure_city = %s AND arrival_city = %s AND DATE(departure_time) = %s")
        params = [departure_city, arrival_city, date]
        if cabin_type:
            sql += " AND cabin_type = %s"
            params.append(cabin_type)
        return self._execute_query(sql, params)

    def query_concert(self, city: str, artist: str, date: str, ticket_type: str = None) -> str:
        """
        查询演唱会/演出票。

        参数:
            city:        演出城市，如 "北京"
            artist:      艺人名称，如 "周杰伦"
            date:        演出日期，格式 "YYYY-MM-DD"
            ticket_type: 票类型（可选），如 "VIP"、"普通票"

        返回:
            JSON 字符串，包含艺人、场馆、时间、票价、余票等信息
        """
        sql = ("SELECT id, artist, city, venue, start_time, end_time, "
               "ticket_type, price, remaining_seats FROM concert_tickets "
               "WHERE city = %s AND artist = %s AND DATE(start_time) = %s")
        params = [city, artist, date]
        if ticket_type:
            sql += " AND ticket_type = %s"
            params.append(ticket_type)
        return self._execute_query(sql, params)

    # --------------------------------------------------------
    # 预定方法（演示环境：仅模拟返回，未实际写入数据库）
    # --------------------------------------------------------

    def order_train(self, departure_date: str, train_number: str, seat_type: str, number: int) -> str:
        """模拟火车票预定，返回预定成功信息。

        参数：
            departure_date: 出发日期，格式 "YYYY-MM-DD"。
            train_number:   车次号，如 "G351"。
            seat_type:      座位类型，如 "二等座"。
            number:         订票数量。

        返回值：
            str: 预定成功提示文本。

        说明：
            这是演示实现，只回显参数、不写数据库；真实接入时应增加库存校验、
            事务和幂等控制（避免重复下单）。
        """
        return json.dumps({"status": "input_required", "data": [], "message": "聊天不执行预订。请在行程方案页核对报价，再确认创建模拟订单；本工具未创建任何订单。"}, ensure_ascii=False)

    def order_flight(self, departure_date: str, flight_number: str, cabin_type: str, number: int) -> str:
        """模拟机票预定，返回预定成功信息。

        参数：
            departure_date: 出发日期，格式 "YYYY-MM-DD"。
            flight_number:  航班号，如 "CA4116"。
            cabin_type:     舱位类型，如 "经济舱"。
            number:         订票数量。

        返回值：
            str: 预定成功提示文本（演示实现，未实际写库）。
        """
        return json.dumps({"status": "input_required", "data": [], "message": "聊天不执行预订。请在行程方案页核对报价，再确认创建模拟订单；本工具未创建任何订单。"}, ensure_ascii=False)

    def order_concert(self, start_date: str, artist: str, venue: str, ticket_type: str, number: int) -> str:
        """模拟演唱会票预定，返回预定成功信息。

        参数：
            start_date:  演出日期，格式 "YYYY-MM-DD"。
            artist:      艺人/乐队名，如 "薛之谦"。
            venue:       演出场馆，如 "成都东安湖体育公园主体育场"。
            ticket_type: 票类型，如 "VIP内场"。
            number:      订票数量。

        返回值：
            str: 预定成功提示文本（演示实现，未实际写库）。
        """
        return json.dumps({"status": "input_required", "data": [], "message": "演唱会预订暂未纳入订单范围，本工具未创建任何订单。"}, ensure_ascii=False)


def create_ticket_mcp_server():
    """创建票务 MCP 工具并通过官方 Streamable HTTP 传输启动服务。"""
    # Flow step 8: MCP 工具层。票务 A2A 只理解任务，真实查询在这里执行。
    # MCP 只暴露业务工具，端口和绑定地址可由部署环境覆盖。
    bind_host = os.getenv("TICKET_MCP_BIND_HOST", "127.0.0.1")
    port = int(os.getenv("TICKET_MCP_PORT", "8001"))
    ticket_mcp = FastMCP(
        name="TicketTools",
        host=bind_host,
        port=port,
        streamable_http_path="/mcp",
    )

    # 工具闭包共享同一个数据库服务实例，避免每次调用重复建立连接。
    service = TicketService()

    # 查询类工具：Agent 根据工具描述自动生成参数并调用对应方法。
    @ticket_mcp.tool(
        name="query_train",
        description="查询火车票信息，参数：departure_city(出发城市), arrival_city(到达城市), date(日期), seat_type(座位类型，可选)"
    )
    def query_train(departure_city: str, arrival_city: str, date: str, seat_type: str = None) -> str:
        """MCP 工具入口：查询指定日期的火车票余票和价格。"""
        return service.query_train(departure_city, arrival_city, date, seat_type)

    @ticket_mcp.tool(
        name="query_flight",
        description="查询机票信息，参数：departure_city(出发城市), arrival_city(到达城市), date(日期), cabin_type(舱位类型，可选)"
    )
    def query_flight(departure_city: str, arrival_city: str, date: str, cabin_type: str = None) -> str:
        """MCP 工具入口：查询指定日期的航班余票和舱位价格。"""
        return service.query_flight(departure_city, arrival_city, date, cabin_type)

    @ticket_mcp.tool(
        name="query_concert",
        description="查询演唱会票信息，参数：city(城市), artist(艺人), date(日期), ticket_type(票类型，可选)"
    )
    def query_concert(city: str, artist: str, date: str, ticket_type: str = None) -> str:
        """MCP 工具入口：查询城市、艺人、日期对应的演唱会票。"""
        return service.query_concert(city, artist, date, ticket_type)

    # 预订类工具：当前业务方法为演示实现，后续可替换为真实写库逻辑。
    @ticket_mcp.tool(
        name="order_train",
        description="根据车次号、日期、座位类型和数量预定火车票。参数：train_number(车次号，如G123), departure_date(出发日期，格式YYYY-MM-DD), seat_type(座位类型，如二等座/一等座/商务座), number(订票数量)"
    )
    def order_train(departure_date: str, train_number: str, seat_type: str, number: int) -> str:
        """MCP 工具入口：演示火车票预订流程。"""
        return service.order_train(departure_date, train_number, seat_type, number)

    @ticket_mcp.tool(
        name="order_flight",
        description="根据航班号、日期、舱位类型和数量预定机票。参数：flight_number(航班号，如CA4101), departure_date(出发日期，格式YYYY-MM-DD), cabin_type(舱位类型，如经济舱/商务舱/头等舱), number(订票数量)"
    )
    def order_flight(departure_date: str, flight_number: str, cabin_type: str, number: int) -> str:
        """MCP 工具入口：演示机票预订流程。"""
        return service.order_flight(departure_date, flight_number, cabin_type, number)

    @ticket_mcp.tool(
        name="order_concert",
        description="根据艺人、日期、场馆、票类型和数量预定演唱会门票。参数：artist(艺人/乐队名), start_date(演出日期，格式YYYY-MM-DD), venue(演出场馆名), ticket_type(票类型，如VIP/普通票), number(订票数量)"
    )
    def order_concert(start_date: str, artist: str, venue: str, ticket_type: str, number: int) -> str:
        """MCP 工具入口：演示演唱会票预订流程。"""
        return service.order_concert(start_date, artist, venue, ticket_type, number)

    logger.info("=== 票务MCP服务器信息 ===")
    logger.info(f"名称: {ticket_mcp.name}")

    try:
        print(f"服务器已启动，请访问 http://{bind_host}:{port}/mcp")
        # 使用官方 MCP SDK 的 Streamable HTTP，不再手工封装 JSON-RPC。
        ticket_mcp.run(transport="streamable-http")
    except Exception:
        logger.exception("票务 MCP 服务器启动失败")
        raise


if __name__ == '__main__':
    # service = TicketService()
    # train = service.query_train("北京", "成都", "2026-05-01", "二等座")
    # print(train)
    # flight = service.query_flight("北京", "成都", "2026-05-01", "头等舱")
    # print(flight)
    create_ticket_mcp_server()
