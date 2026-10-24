"""
需求：管理SmartVoyage项目的配置信息，包括大模型、数据库、日志等配置
思路步骤：
1. 定义项目根目录路径
2. 支持从本地 .env 读取运行参数
3. 创建Config类管理所有配置项
4. 配置大模型参数（API地址、密钥、模型名称）
5. 配置数据库参数（主机、用户名、密码、数据库名）
6. 配置日志文件路径
7. 配置票务查询接口地址
8. 配置意图映射字典
9. 提供数据库连接参数访问方法
"""

import os

# 项目根目录用于生成默认日志路径，避免日志写到当前工作目录的不确定位置。
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 天气爬虫默认城市：中文名 -> 和风 LocationID。Open-Meteo 兜底只需要城市名。
DEFAULT_WEATHER_CITY_CODES = {
    "北京": "101010100",
    "成都": "101270101",
    "都江堰": "101270111",
    "乐山": "101271401",
    "德阳": "101272001",
    "绵阳": "101270401",
    "上海": "101020100",
    "武汉": "101200101",
}


def parse_weather_city_codes(raw: str | None) -> dict[str, str]:
    """解析 SMARTVOYAGE_WEATHER_CITIES，格式：北京:101010100,成都:101270101。"""
    if not raw or not raw.strip():
        return dict(DEFAULT_WEATHER_CITY_CODES)

    cities: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, code = item.split(":", 1)
            name, code = name.strip(), code.strip()
            if name:
                cities[name] = code
        else:
            cities[item] = DEFAULT_WEATHER_CITY_CODES.get(item, "")
    return cities or dict(DEFAULT_WEATHER_CITY_CODES)


def _load_local_env():
    """加载项目根目录下的 .env，方便本地演示时统一配置密钥和端口。"""
    env_path = os.path.join(project_root, ".env")
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_local_env()


class Config:
    """集中管理项目配置，敏感信息优先从环境变量读取。"""
    def __init__(self):
        """读取并固化一次运行所需的全部配置。

        配置来源优先级：
            1. 操作系统环境变量。
            2. 项目根目录 ``.env`` 中加载到环境变量的值。
            3. 代码中的本地演示默认值。

        这里不做网络连接，也不校验密钥是否真实有效；具体连接错误交给 LLM、
        MySQL、MCP、Milvus 等调用点暴露，便于定位是哪一层配置出了问题。
        """
        # 大模型配置：兼容 DashScope 等 OpenAI API 兼容服务。
        self.base_url = os.getenv(
            "LLM_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self.api_key = (
            os.getenv("DASHSCOPE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        )
        self.model_name = os.getenv("LLM_MODEL_NAME", "qwen-plus")

        # 数据库配置：连接超时较短，避免依赖未启动时阻塞整个服务。
        self.host = os.getenv("SMARTVOYAGE_DB_HOST", "127.0.0.1")
        self.port = int(os.getenv("SMARTVOYAGE_DB_PORT", "3306"))
        self.user = os.getenv("SMARTVOYAGE_DB_USER", "root")
        self.password = os.getenv("SMARTVOYAGE_DB_PASSWORD", "")
        self.database = os.getenv("SMARTVOYAGE_DB_NAME", "travel_rag")
        self.db_connect_timeout = int(
            os.getenv("SMARTVOYAGE_DB_CONNECT_TIMEOUT", "5")
        )

        # 日志配置：可通过环境变量将日志重定向到部署目录。
        self.log_file = os.getenv(
            "SMARTVOYAGE_LOG_FILE",
            os.path.join(project_root, "logs", "app.log"),
        )

        # # 票务查询的12306接口地址
        # self.url_123 = ""

        # 路由表：主 Agent 根据意图把任务转发到对应的 A2A 服务。
        self.intent = {
            "weather": "WeatherQueryAssistant",
            "flight": "TicketAssistant",
            "train": "TicketAssistant",
            "concert": "TicketAssistant",
            "order": "TicketAssistant",
            "car_rental": "TripAssistant",
            "tour_group": "TripAssistant",
            "insurance": "TripAssistant",
            "trip_order": "TripAssistant",
        }

        self.temperature = 0.1

        # 天气数据源配置：api 不依赖 MySQL，db 由天气爬虫定时写入数据库。
        self.weather_source = os.getenv("SMARTVOYAGE_WEATHER_SOURCE", "db").lower()

        # 和风天气 API 配置，密钥不写入源码。
        self.weather_api_key = os.getenv("QWEATHER_API_KEY", "")
        self.weather_base_url = os.getenv(
            "QWEATHER_BASE_URL",
            "https://j246h349g7.re.qweatherapi.com/v7/weather/30d",
        )
        self.weather_api_host = os.getenv(
            "QWEATHER_API_HOST",
            "geoapi.qweather.com",
        )
        self.weather_timezone = os.getenv("SMARTVOYAGE_TIMEZONE", "Asia/Shanghai")

        # 天气监控城市列表（城市名 -> 城市代码），爬虫按这个列表拉未来预报。
        self.weather_city_codes = parse_weather_city_codes(
            os.getenv("SMARTVOYAGE_WEATHER_CITIES")
        )

        # 天气定时更新：默认每 10 分钟；INTERVAL_MINUTES=0 时退回每天定点。
        self.weather_schedule_time = os.getenv(
            "SMARTVOYAGE_WEATHER_SCHEDULE_TIME",
            "01:00",
        )
        interval_minutes = os.getenv("SMARTVOYAGE_WEATHER_INTERVAL_MINUTES", "").strip()
        interval_hours = os.getenv("SMARTVOYAGE_WEATHER_INTERVAL_HOURS", "").strip()
        if interval_minutes:
            self.weather_interval_minutes = int(interval_minutes)
        elif interval_hours:
            self.weather_interval_minutes = int(interval_hours) * 60
        else:
            self.weather_interval_minutes = 10
        self.weather_interval_hours = self.weather_interval_minutes // 60

        # Milvus 向量数据库配置，用于旅游团语义检索。
        self.milvus_host = os.getenv("MILVUS_HOST", "127.0.0.1")
        self.milvus_port = int(os.getenv("MILVUS_PORT", "19530"))
        self.tour_group_collection = os.getenv(
            "MILVUS_COLLECTION",
            "tour_groups",
        )

        # Qwen Embedding API 配置
        self.embedding_url = os.getenv(
            "EMBEDDING_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
        )
        self.embedding_dim = int(os.getenv("EMBEDDING_DIM", "1024"))

    def get_mysql_config(self):
        """返回当前数据库配置。"""
        return self.host, self.user, self.password, self.database


if __name__ == '__main__':
    print(Config().log_file)
    print(Config().get_mysql_config())
