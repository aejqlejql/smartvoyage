"""
需求：实现天气数据爬虫，定期从第三方API获取天气数据并存储到MySQL数据库
思路步骤：
1. 导入必要的模块和库
2. 配置API密钥、城市代码和数据库连接参数
3. 实现fetch_weather_data函数（从API获取天气数据）
4. 实现get_latest_update_time函数（获取数据库中最新更新时间）
5. 实现should_update_data函数（判断是否需要更新数据）
6. 实现store_weather_data函数（将天气数据存储到数据库）
7. 实现update_weather函数（更新指定城市的天气数据）
8. 实现setup_scheduler函数（设置定时任务）
9. 主函数（创建表、执行一次更新、启动定时任务）
"""
import json
import time
from datetime import datetime

import mysql.connector
import pytz
import requests
import schedule

if __package__:
    from ..infrastructure.config import Config
else:
    # 允许 PyCharm 直接运行本文件；推荐命令仍是：
    # python -m ai01_SmartVoyage.utils.spider_weather
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ai01_SmartVoyage.infrastructure.config import Config

cfg = Config()
TZ = pytz.timezone(cfg.weather_timezone)


def get_db_config():
    """生成 mysql.connector 可直接使用的连接参数。

    天气爬虫和 MCP 查询服务共享 Config 中的数据库配置，避免脚本里出现硬编码账号。
    """
    return {
        "host": cfg.host,
        "port": cfg.port,
        "user": cfg.user,
        "password": cfg.password,
        "database": cfg.database,
        "connection_timeout": cfg.db_connect_timeout,
        "charset": "utf8mb4",
    }


def connect_db():
    """创建天气爬虫使用的 MySQL 连接。"""
    return mysql.connector.connect(**get_db_config())


GEOCODE_ALIASES = {
    "都江堰": "Dujiangyan",
    "乐山": "Leshan",
    "德阳": "Deyang",
    "绵阳": "Mianyang",
    "武汉": "Wuhan",
}

# 中央气象台站点检索：先查这些省，避免每次扫全国。
NMC_PROVINCE_HINTS = {
    "北京": "ABJ",
    "上海": "ASH",
    "武汉": "AHB",
    "成都": "ASC",
    "都江堰": "ASC",
    "乐山": "ASC",
    "德阳": "ASC",
    "绵阳": "ASC",
}
NMC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.nmc.cn/",
    "Accept": "application/json,text/plain,*/*",
}
_nmc_station_cache: dict[str, str] = {}

WMO_WEATHER_TEXT = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾",
    51: "小雨",
    53: "中雨",
    55: "大雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "阵雨",
    82: "暴雨",
    95: "雷阵雨",
    96: "雷阵雨",
    99: "雷阵雨",
}


def _wind_dir_text(degree) -> str:
    """把 0-360 风向角转成中文风向。"""
    if degree is None:
        return ""
    labels = ["北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风"]
    return labels[int((float(degree) + 22.5) // 45) % 8]


def _nmc_clean(value):
    """中央气象台用 9999 表示缺测。"""
    if value is None or value == "":
        return None
    if value in (9999, 9999.0, "9999"):
        return None
    return value


def _nmc_get(url, params=None):
    response = requests.get(url, params=params, headers=NMC_HEADERS, timeout=15)
    response.raise_for_status()
    return response.json()


def resolve_nmc_station(city, province_hint=None):
    """按中文城市名解析中央气象台 stationid。"""
    if city in _nmc_station_cache:
        return _nmc_station_cache[city]

    try:
        provinces = _nmc_get("https://www.nmc.cn/rest/province")
    except (requests.RequestException, json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"{city} 中央气象台省份列表失败: {exc}")
        return None

    hinted = province_hint or NMC_PROVINCE_HINTS.get(city)
    ordered = []
    for item in provinces or []:
        code = (item or {}).get("code")
        if not code:
            continue
        if hinted and code == hinted:
            ordered.insert(0, code)
        else:
            ordered.append(code)

    for province_code in ordered:
        try:
            cities = _nmc_get(f"https://www.nmc.cn/rest/province/{province_code}")
        except (requests.RequestException, json.JSONDecodeError, ValueError, TypeError):
            continue
        for item in cities or []:
            name = (item or {}).get("city")
            station = (item or {}).get("code")
            if name and station:
                _nmc_station_cache.setdefault(name, station)
        if city in _nmc_station_cache:
            return _nmc_station_cache[city]
        if hinted and province_code == hinted:
            break

    print(f"{city} 中央气象台未找到站点")
    return None


def parse_nmc_weather(payload, city):
    """把中央气象台 /rest/weather 响应对齐到和风 daily 结构。"""
    data = (payload or {}).get("data") or {}
    predict = data.get("predict") or {}
    details = predict.get("detail") or []
    if not details:
        return None

    real = data.get("real") or {}
    sunrise_info = real.get("sunriseSunset") or {}
    real_weather = real.get("weather") or {}
    publish = predict.get("publish_time") or real.get("publish_time")
    if publish:
        update_time = str(publish).replace(" ", "T")
        if "+" not in update_time:
            update_time += "+08:00"
    else:
        update_time = datetime.now(TZ).isoformat()

    rows = []
    for item in details:
        fx_date = item.get("date")
        if not fx_date:
            continue
        day = item.get("day") or {}
        night = item.get("night") or {}
        day_weather = day.get("weather") or {}
        night_weather = night.get("weather") or {}
        day_wind = day.get("wind") or {}
        night_wind = night.get("wind") or {}
        sunrise = None
        sunset = None
        raw_sunrise = sunrise_info.get("sunrise") or ""
        raw_sunset = sunrise_info.get("sunset") or ""
        if str(raw_sunrise).startswith(fx_date):
            sunrise = str(raw_sunrise)[11:16]
        if str(raw_sunset).startswith(fx_date):
            sunset = str(raw_sunset)[11:16]
        humidity = None
        real_publish = str(real.get("publish_time") or "")
        if real_publish.startswith(fx_date):
            humidity = _nmc_clean(real_weather.get("humidity"))

        rows.append({
            "fxDate": fx_date,
            "sunrise": sunrise,
            "sunset": sunset,
            "moonrise": None,
            "moonset": None,
            "moonPhase": None,
            "moonPhaseIcon": None,
            "tempMax": _nmc_clean(day_weather.get("temperature")),
            "tempMin": _nmc_clean(night_weather.get("temperature")),
            "iconDay": str(_nmc_clean(day_weather.get("img")) or ""),
            "textDay": _nmc_clean(day_weather.get("info")) or "",
            "iconNight": str(_nmc_clean(night_weather.get("img")) or ""),
            "textNight": _nmc_clean(night_weather.get("info")) or "",
            "wind360Day": None,
            "windDirDay": _nmc_clean(day_wind.get("direct")) or "",
            "windScaleDay": _nmc_clean(day_wind.get("power")),
            "windSpeedDay": None,
            "wind360Night": None,
            "windDirNight": _nmc_clean(night_wind.get("direct")) or "",
            "windScaleNight": _nmc_clean(night_wind.get("power")),
            "windSpeedNight": None,
            "humidity": humidity,
            "precip": _nmc_clean(item.get("precipitation")),
            "pressure": None,
            "vis": None,
            "cloud": None,
            "uvIndex": None,
        })

    if not rows:
        return None
    return {
        "code": "200",
        "updateTime": update_time,
        "daily": rows,
        "source": "nmc",
    }


def fetch_weather_from_nmc(city):
    """从中央气象台拉取城市预报（官网 JSON，约 7 天）。"""
    try:
        station = resolve_nmc_station(city)
        if not station:
            return None
        payload = _nmc_get(
            "https://www.nmc.cn/rest/weather",
            params={"stationid": station},
        )
        if (payload or {}).get("code") not in (0, "0", None) and (payload or {}).get("msg") != "success":
            print(f"{city} 中央气象台返回异常: {payload.get('msg')}")
            return None
        parsed = parse_nmc_weather(payload, city)
        if not parsed:
            print(f"{city} 中央气象台没有可用预报")
            return None
        print(f"{city} 已从中央气象台获取 {len(parsed.get('daily') or [])} 天预报")
        return parsed
    except requests.RequestException as exc:
        print(f"请求 {city} 中央气象台失败: {exc}")
        return None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"{city} 中央气象台解析失败: {exc}")
        return None


def fetch_weather_from_qweather(city, location):
    """从和风天气拉取指定城市未来约 30 天预报。"""
    if not cfg.weather_api_key:
        return None
    if not location:
        print(f"{city} 缺少和风 LocationID，跳过和风接口。")
        return None

    headers = {"X-QW-Api-Key": cfg.weather_api_key}
    try:
        response = requests.get(
            cfg.weather_base_url,
            headers=headers,
            params={"location": location},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != "200":
            print(f"{city} 和风天气返回异常: {data.get('code')}")
            return None
        print(f"{city} 已从和风天气获取 {len(data.get('daily') or [])} 天预报")
        return data
    except requests.RequestException as exc:
        print(f"请求 {city} 和风天气失败: {exc}")
        return None
    except json.JSONDecodeError as exc:
        print(f"{city} 和风天气 JSON 解析失败: {exc}")
        return None


def fetch_weather_from_open_meteo(city):
    """没有和风 Key 时，用 Open-Meteo 免费接口按城市名拉预报。

    免费接口最多约 16 天，字段对齐到和风 daily 结构，后续入库逻辑不用改。
    """
    try:
        search_names = [city]
        alias = GEOCODE_ALIASES.get(city)
        if alias:
            search_names.append(alias)
        results = []
        for name in search_names:
            geo = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": name, "count": 1, "language": "zh", "format": "json"},
                timeout=15,
            )
            geo.raise_for_status()
            results = (geo.json() or {}).get("results") or []
            if results:
                break
        if not results:
            print(f"{city} Open-Meteo 未找到地理坐标")
            return None
        lat, lon = results[0]["latitude"], results[0]["longitude"]

        forecast = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": ",".join([
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_sum",
                    "relative_humidity_2m_mean",
                    "wind_speed_10m_max",
                    "wind_direction_10m_dominant",
                    "cloud_cover_mean",
                    "uv_index_max",
                    "sunrise",
                    "sunset",
                    "visibility_mean",
                ]),
                "forecast_days": 16,
                "timezone": cfg.weather_timezone,
            },
            timeout=15,
        )
        forecast.raise_for_status()
        daily = (forecast.json() or {}).get("daily") or {}
        dates = daily.get("time") or []
        rows = []
        for index, fx_date in enumerate(dates):
            code = (daily.get("weather_code") or [None])[index]
            text = WMO_WEATHER_TEXT.get(int(code) if code is not None else -1, "多云")
            wind_dir = _wind_dir_text((daily.get("wind_direction_10m_dominant") or [None])[index])
            sunrise = (daily.get("sunrise") or [""])[index]
            sunset = (daily.get("sunset") or [""])[index]
            vis = (daily.get("visibility_mean") or [None])[index]
            rows.append({
                "fxDate": fx_date,
                "sunrise": sunrise[11:16] if sunrise else None,
                "sunset": sunset[11:16] if sunset else None,
                "moonrise": None,
                "moonset": None,
                "moonPhase": None,
                "moonPhaseIcon": None,
                "tempMax": (daily.get("temperature_2m_max") or [None])[index],
                "tempMin": (daily.get("temperature_2m_min") or [None])[index],
                "iconDay": str(code) if code is not None else None,
                "textDay": text,
                "iconNight": str(code) if code is not None else None,
                "textNight": text,
                "wind360Day": (daily.get("wind_direction_10m_dominant") or [None])[index],
                "windDirDay": wind_dir,
                "windScaleDay": None,
                "windSpeedDay": (daily.get("wind_speed_10m_max") or [None])[index],
                "wind360Night": (daily.get("wind_direction_10m_dominant") or [None])[index],
                "windDirNight": wind_dir,
                "windScaleNight": None,
                "windSpeedNight": (daily.get("wind_speed_10m_max") or [None])[index],
                "humidity": (daily.get("relative_humidity_2m_mean") or [None])[index],
                "precip": (daily.get("precipitation_sum") or [None])[index],
                "pressure": None,
                "vis": int(vis / 1000) if vis else None,
                "cloud": (daily.get("cloud_cover_mean") or [None])[index],
                "uvIndex": (daily.get("uv_index_max") or [None])[index],
            })
        print(f"{city} 已从 Open-Meteo 获取 {len(rows)} 天预报")
        return {
            "code": "200",
            "updateTime": datetime.now(TZ).isoformat(),
            "daily": rows,
        }
    except requests.RequestException as exc:
        print(f"请求 {city} Open-Meteo 失败: {exc}")
        return None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"{city} Open-Meteo 解析失败: {exc}")
        return None


def fetch_weather_data(city, location):
    """优先中央气象台，其次和风 30 天，最后 Open-Meteo。"""
    data = fetch_weather_from_nmc(city)
    if data:
        return data
    print(f"{city} 中央气象台不可用，尝试备用源")
    data = fetch_weather_from_qweather(city, location)
    if data:
        return data
    if not cfg.weather_api_key:
        print(f"{city} 未配置 QWEATHER_API_KEY，改用 Open-Meteo")
    else:
        print(f"{city} 和风天气不可用，改用 Open-Meteo")
    return fetch_weather_from_open_meteo(city)


def get_latest_update_time(cursor, city):
    """
    拿取 这个 城市city 在数据表weather_data中最新更新时间。
    :param cursor:
    :param city:
    :return: datetime对象 或 None

    fetchone() 返回单个元组，元素顺序与 SELECT 列顺序一致：
        # cursor.execute("SELECT city, MAX(update_time) FROM weather_data WHERE city = %s", (city,))
        # result = cursor.fetchone()
        # result => ("北京", datetime.datetime(2026, 4, 28, 10, 30, 0))
        # result[0] => "北京"
        # result[1] => datetime.datetime(2026, 4, 28, 10, 30, 0)

        # 如果查询无结果（表中无该城市数据）：
        # result => (None,)

    fetchall() 返回元组列表，适用于多行结果：
        # cursor.execute("SELECT city, update_time FROM weather_data WHERE city = %s", (city,))
        # results = cursor.fetchall()
        # results => [
        #     ("北京", datetime.datetime(2026, 4, 27, 10, 30, 0)),
        #     ("北京", datetime.datetime(2026, 4, 28, 10, 30, 0))
        # ]
    """
    cursor.execute("SELECT MAX(update_time) FROM weather_data WHERE city = %s", (city,))
    result = cursor.fetchone()
    # 本查询只返回一个聚合值，result 示例 => (datetime.datetime(2026, 4, 28, 10, 30, 0),)
    # 若无数据 => (None,)
    return result[0] if result[0] else None


def should_update_data(latest_time, force_update=False, min_age_minutes=None):
    """距上次入库超过调度间隔（默认 10 分钟）才重新拉取。"""
    if force_update:
        return True
    if not latest_time:
        return True
    age_minutes = cfg.weather_interval_minutes if min_age_minutes is None else min_age_minutes
    if age_minutes <= 0:
        age_minutes = 24 * 60
    current_time = datetime.now(TZ)
    if latest_time.tzinfo is None:
        latest_time = TZ.localize(latest_time)
    else:
        latest_time = latest_time.astimezone(TZ)
    return (current_time - latest_time).total_seconds() / 60 >= age_minutes


def store_weather_data(conn, cursor, city, data):
    """
    把从第三方拿到的天气数据（json）解析出来，组装成可以插入到mysql天气表中的insert语句。然后执行插入动作。
    :param conn:
    :param cursor:
    :param city:
    :param data:
    :return:
    """

    # {"code":"200",
    # "updateTime":"2026-02-04T10:42+08:00",
    # "fxLink":"https://www.qweather.com/weather/beijing-101010100.html",
    # "daily":[
    #     {"fxDate":"2026-02-04","sunrise":"07:21","sunset":"17:37","moonrise":"20:21",
    #     "moonset":"08:41","moonPhase":"亏凸月","moonPhaseIcon":"805","tempMax":"12",
    #     "tempMin":"-2","iconDay":"100","textDay":"晴","iconNight":"150","textNight":"晴",
    #     "wind360Day":"225","windDirDay":"西南风","windScaleDay":"1-3","windSpeedDay":"3",
    #     "wind360Night":"0","windDirNight":"北风","windScaleNight":"1-3","windSpeedNight":"16",
    #     "humidity":"29","precip":"0.0","pressure":"1016","vis":"25","cloud":"0","uvIndex":"3"}
    #     。。。。
    # "refer":{"sources":["QWeather"],"license":["QWeather Developers License"]}}

    if not data or data.get("code") != "200":
        print(f"{city} 数据无效，跳过存储。")
        return

    daily_data = data.get("daily", [])
    raw_update = data.get("updateTime") or datetime.now(TZ).isoformat()
    update_time = datetime.fromisoformat(str(raw_update).replace("Z", "+00:00"))
    if update_time.tzinfo is None:
        update_time = TZ.localize(update_time)
    update_time = update_time.astimezone(TZ).replace(tzinfo=None)

    for day in daily_data:
        # {"fxDate":"2026-02-04","sunrise":"07:21","sunset":"17:37","moonrise":"20:21",
        #     #     "moonset":"08:41","moonPhase":"亏凸月","moonPhaseIcon":"805","tempMax":"12",
        #     #     "tempMin":"-2","iconDay":"100","textDay":"晴","iconNight":"150","textNight":"晴",
        #     #     "wind360Day":"225","windDirDay":"西南风","windScaleDay":"1-3","windSpeedDay":"3",
        #     #     "wind360Night":"0","windDirNight":"北风","windScaleNight":"1-3","windSpeedNight":"16",
        #     #     "humidity":"29","precip":"0.0","pressure":"1016","vis":"25","cloud":"0","uvIndex":"3"}
        fx_date = datetime.strptime(day["fxDate"], "%Y-%m-%d").date()
        values = (
            city,
            fx_date,
            day.get("sunrise"), day.get("sunset"),
            day.get("moonrise"), day.get("moonset"),
            day.get("moonPhase"), day.get("moonPhaseIcon"),
            day.get("tempMax"), day.get("tempMin"),
            day.get("iconDay"), day.get("textDay"),
            day.get("iconNight"), day.get("textNight"),
            day.get("wind360Day"), day.get("windDirDay"), day.get("windScaleDay"), day.get("windSpeedDay"),
            day.get("wind360Night"), day.get("windDirNight"), day.get("windScaleNight"), day.get("windSpeedNight"),
            day.get("precip"), day.get("uvIndex"),
            day.get("humidity"), day.get("pressure"),
            day.get("vis"), day.get("cloud"),
            update_time
        )
        # upsert (Update + Insert): 插入或更新操作，一条SQL同时处理两种情况
        # 原理：依赖表的唯一键约束（本表中为 UNIQUE KEY unique_city_date(city, fx_date)）
        #   - INSERT 路径：表中不存在该 (city, fx_date) 组合 → 直接插入新行
        #   - UPDATE 路径：表中已存在该 (city, fx_date) 组合 → 用新值覆盖旧值（ON DUPLICATE KEY UPDATE）
        #
        # 示例：
        #   首次拉取 ("北京", 2026-04-28) → INSERT 新行
        #   再次拉取 ("北京", 2026-04-28) → UPDATE 该行所有天气字段，同时更新 update_time
        #
        # VALUES(col) 表示 "要插入的那份新值"，而非表中原有的旧值
        insert_query = """
                       INSERT INTO weather_data (city, fx_date, sunrise, sunset, moonrise, moonset, moon_phase,
                                                 moon_phase_icon,
                                                 temp_max, temp_min, icon_day, text_day, icon_night, text_night,
                                                 wind360_day, wind_dir_day, wind_scale_day, wind_speed_day,
                                                 wind360_night, wind_dir_night, wind_scale_night, wind_speed_night,
                                                 precip, uv_index, humidity, pressure, vis, cloud, update_time)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE sunrise          = VALUES(sunrise),
                                               sunset           = VALUES(sunset),
                                               moonrise         = VALUES(moonrise),
                                               moonset          = VALUES(moonset),
                                               moon_phase       = VALUES(moon_phase),
                                               moon_phase_icon  = VALUES(moon_phase_icon),
                                               temp_max         = VALUES(temp_max),
                                               temp_min         = VALUES(temp_min),
                                               icon_day         = VALUES(icon_day),
                                               text_day         = VALUES(text_day),
                                               icon_night       = VALUES(icon_night),
                                               text_night       = VALUES(text_night),
                                               wind360_day      = VALUES(wind360_day),
                                               wind_dir_day     = VALUES(wind_dir_day),
                                               wind_scale_day   = VALUES(wind_scale_day),
                                               wind_speed_day   = VALUES(wind_speed_day),
                                               wind360_night    = VALUES(wind360_night),
                                               wind_dir_night   = VALUES(wind_dir_night),
                                               wind_scale_night = VALUES(wind_scale_night),
                                               wind_speed_night = VALUES(wind_speed_night),
                                               precip           = VALUES(precip),
                                               uv_index         = VALUES(uv_index),
                                               humidity         = VALUES(humidity),
                                               pressure         = VALUES(pressure),
                                               vis              = VALUES(vis),
                                               cloud            = VALUES(cloud),
                                               update_time      = VALUES(update_time) \
                       """
        try:
            cursor.execute(insert_query, values)
            print(f"{city} {fx_date} 数据写入/更新成功: {day.get('textDay')}, 影响行数: {cursor.rowcount}")
        except mysql.connector.Error as e:
            print(f"{city} {fx_date} 数据库错误: {e}")
            conn.rollback()
            print(f"{city} 事务回滚。")
            return

    conn.commit()
    print(f"{city} 共写入 {len(daily_data)} 天，事务已提交。")


def update_weather(force_update=False):
    """
    遍历配置中的所有城市，判断是否需要拉取最新天气数据并入库。

    流程图：
        开始
         │
         ├── connect_db() ──► 建立 MySQL 连接
         │
         ├── 遍历 cfg.weather_city_codes 中的每个 (city, location)
         │    │
         │    ├── get_latest_update_time(cursor, city)
         │    │    └── SELECT MAX(update_time) FROM weather_data
         │    │         └──► latest_time = datetime / None
         │    │
         │    ├── should_update_data(latest_time, force_update) 判断：
         │    │    ├── force_update == True       ──► True（强制更新）
         │    │    ├── latest_time == None         ──► True（首次拉取）
         │    │    └── 距今 >= 调度间隔            ──► True（数据过期）
         │    │    └── 其他情况                    ──► False（跳过）
         │    │
         │    ├── True 分支（需要更新）:
         │    │    ├── fetch_weather_data(city, location)
         │    │    │    └──► HTTP GET → 和风天气 API → gzip 解压 → 返回 JSON dict
         │    │    │
         │    │    ├── data 有效？
         │    │    │    ├── 是 ──► store_weather_data(conn, cursor, city, data)
         │    │    │    │          └──► 解析 daily 数组 → 逐条 UPSERT → commit
         │    │    │    └── 否 ──► 跳过该城市（打印警告）
         │    │
         │    └── False 分支（数据已最新）:
         │         └──► 打印 "数据已为最新，无需更新"
         │
         └── cursor.close() / conn.close() ──► 结束
    """
    conn = connect_db()
    cursor = conn.cursor()

    for city, location in cfg.weather_city_codes.items():
        # 拿取 这个 城市city 在数据表weather_data中最新更新时间。
        latest_time = get_latest_update_time(cursor, city)
        # 没有更新时间、超过调度间隔、或 force_update=True 时才重新拉取。
        # 其他情况返回False，表示不更新数据库中的数据。
        if should_update_data(latest_time, force_update):
            print(f"开始更新 {city} 天气数据...")
            # 从第三方API拿取天气数据
            data = fetch_weather_data(city, location)
            if data:
                # 把从第三方拿到的天气数据（json）解析出来，组装成可以插入到mysql天气表中的insert语句。然后执行插入动作。
                store_weather_data(conn, cursor, city, data)
        else:
            print(f"{city} 数据已为最新，无需更新。最新更新时间: {latest_time}")

    cursor.close()
    conn.close()


def setup_scheduler():
    """启动天气数据定时更新循环。

    只有 ``SMARTVOYAGE_WEATHER_SOURCE`` 不是 ``api`` 时才需要爬取并写入数据库；
    API 模式下实时查询和风天气，不启动本地定时任务。
    """
    # 保护性代码：只要是 API 模式，就不启动数据库写入调度。
    if cfg.weather_source == 'api':
        return

    if cfg.weather_interval_minutes > 0:
        schedule.every(cfg.weather_interval_minutes).minutes.do(update_weather)
        print(f"天气爬虫已启动，每隔 {cfg.weather_interval_minutes} 分钟更新一次。")
    else:
        schedule.every().day.at(cfg.weather_schedule_time).do(update_weather)
        print(f"天气爬虫已启动，每天 {cfg.weather_schedule_time} 更新一次。")
    while True:
        schedule.run_pending()
        time.sleep(60)


CREATE_WEATHER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS weather_data
(
    id               INT AUTO_INCREMENT PRIMARY KEY,
    city             VARCHAR(50) NOT NULL COMMENT '城市名称',
    fx_date          DATE        NOT NULL COMMENT '预报日期',
    sunrise          TIME COMMENT '日出时间',
    sunset           TIME COMMENT '日落时间',
    moonrise         TIME COMMENT '月升时间',
    moonset          TIME COMMENT '月落时间',
    moon_phase       VARCHAR(20) COMMENT '月相名称',
    moon_phase_icon  VARCHAR(10) COMMENT '月相图标代码',
    temp_max         INT COMMENT '最高温度',
    temp_min         INT COMMENT '最低温度',
    icon_day         VARCHAR(10) COMMENT '白天天气图标代码',
    text_day         VARCHAR(20) COMMENT '白天天气描述',
    icon_night       VARCHAR(10) COMMENT '夜间天气图标代码',
    text_night       VARCHAR(20) COMMENT '夜间天气描述',
    wind360_day      INT COMMENT '白天风向360角度',
    wind_dir_day     VARCHAR(20) COMMENT '白天风向',
    wind_scale_day   VARCHAR(10) COMMENT '白天风力等级',
    wind_speed_day   INT COMMENT '白天风速 (km/h)',
    wind360_night    INT COMMENT '夜间风向360角度',
    wind_dir_night   VARCHAR(20) COMMENT '夜间风向',
    wind_scale_night VARCHAR(10) COMMENT '夜间风力等级',
    wind_speed_night INT COMMENT '夜间风速 (km/h)',
    precip           DECIMAL(5, 1) COMMENT '降水量 (mm)',
    uv_index         INT COMMENT '紫外线指数',
    humidity         INT COMMENT '相对湿度 (%)',
    pressure         INT COMMENT '大气压强 (hPa)',
    vis              INT COMMENT '能见度 (km)',
    cloud            INT COMMENT '云量 (%)',
    update_time      DATETIME COMMENT '数据更新时间',
    UNIQUE KEY unique_city_date (city, fx_date)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci COMMENT ='天气数据表'
"""


def ensure_weather_table():
    """保证 weather_data 表存在，便于单独启动爬虫。"""
    with mysql.connector.connect(**get_db_config()) as conn:
        cursor = conn.cursor()
        cursor.execute(CREATE_WEATHER_TABLE_SQL)
        conn.commit()
        cursor.close()


def main(argv=None):
    """命令行入口：立刻拉一轮城市列表预报，可选常驻定时。"""
    import argparse

    parser = argparse.ArgumentParser(description="按城市列表爬取天气预报并写入本地 MySQL")
    parser.add_argument("--once", action="store_true", help="只更新一次后退出")
    parser.add_argument("--force", action="store_true", help="忽略 24 小时缓存，强制拉取")
    args = parser.parse_args(argv)

    if cfg.weather_source != "db":
        raise SystemExit(
            "天气爬虫只适用于数据库模式，请设置 SMARTVOYAGE_WEATHER_SOURCE=db。"
        )

    print("监控城市: " + ", ".join(cfg.weather_city_codes.keys()))
    ensure_weather_table()
    update_weather(force_update=args.force or args.once)
    if args.once:
        return
    setup_scheduler()


if __name__ == "__main__":
    main()

# 真实应该怎么更新：
# 1 用户来查询某个城市 比如 北京的天气数据。
# 2 去数据库中查询北京天气数据，select weather_content,update_time from weather_data where city = "北京" and date = "2026-03-04"
# 3 检验这条数据的update_time，如果是1小时之前的，就从第三方api拉取最新数据，返回给用户。同时更新mysql数据。
# 4 如果这条数据是1小时之内更新的，就直接返回给用户。
# 5 做一个缓存，缓存的key是 city+date，设置过期时间ttl，当前时间-更新时间（1小时之内）

# 以上是第一种方式
# 其他方式：
# 从redis中查，有就用，没有就查第三方api，缓存redis，设置ttl=1小时。


# {"code":"200",
# "updateTime":"2026-02-04T10:42+08:00",
# "fxLink":"https://www.qweather.com/weather/beijing-101010100.html",
# "daily":[
#     {"fxDate":"2026-02-04","sunrise":"07:21","sunset":"17:37","moonrise":"20:21",
#     "moonset":"08:41","moonPhase":"亏凸月","moonPhaseIcon":"805","tempMax":"12",
#     "tempMin":"-2","iconDay":"100","textDay":"晴","iconNight":"150","textNight":"晴",
#     "wind360Day":"225","windDirDay":"西南风","windScaleDay":"1-3","windSpeedDay":"3",
#     "wind360Night":"0","windDirNight":"北风","windScaleNight":"1-3","windSpeedNight":"16",
#     "humidity":"29","precip":"0.0","pressure":"1016","vis":"25","cloud":"0","uvIndex":"3"}
#     。。。。
# "refer":{"sources":["QWeather"],"license":["QWeather Developers License"]}}
