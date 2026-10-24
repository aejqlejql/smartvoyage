"""
手工验证和风天气接口的脚本。

用途：
    在开发/调试阶段直接测试和风天气 API 是否可用，验证返回数据格式是否正确。
    该脚本独立于主服务，不依赖 MySQL、MCP 或 A2A，只需一个有效的 API Key。

运行前准备：
    必须先设置环境变量 QWEATHER_API_KEY，避免把密钥硬编码进源码。

使用方式（Windows 命令行）：
    set QWEATHER_API_KEY=your_api_key
    python -m ai01_SmartVoyage.scripts.qweather_probe

说明：
    - 脚本里预置了北京、成都下属各区的城市代码列表，方便按需切换测试城市。
    - 默认查询成都主城区（城市代码 101270101）。
"""

import os

import requests

# 和风天气 API 地址（可通过环境变量覆盖，默认查询未来 3 天天气）
url = os.getenv("QWEATHER_TEST_URL", "https://k544uav444.re.qweatherapi.com/v7/weather/3d")

# 读取 API Key，未配置则直接退出并给出提示
api_key = os.getenv("QWEATHER_API_KEY")
if not api_key:
    raise SystemExit("请先设置 QWEATHER_API_KEY 环境变量")

# 请求头：API Key 通过 X-QW-Api-Key 传递（和风天气的认证方式）
# Accept-Encoding 声明支持 gzip 等压缩，减少传输数据量
headers = {
    "X-QW-Api-Key": api_key,
    "Accept-Encoding": "gzip, deflate, br",
}

# 成都下属各区的城市代码列表（101270101 为成都主城区）
# 和风天气用 LocationID 而非城市名来定位城市
chengdu = [
    101270101, 101270102, 101270103, 101270104, 101270105,
    101270106, 101270107, 101270108, 101270109, 101270110,
    101270111, 101270112, 101270113, 101270114, 101270115,
    101270116, 101270117, 101270118, 101270119, 101270120,
    101270121
]

# 北京下属各区的城市代码列表（101010100 为北京主城区）
beijing = [
    101010100,
    101010200,
    101010300,
    101010400,
    101010500,
    101010600,
    101010700,
    101010800,
    101010900,
    101011000,
    101011100,
    101011200,
    101011300,
    101011400,
    101011500,
    101011600,
    101011700
]

# 查询参数：location 为和风天气的城市 ID
params = {
    'location': '101270101'  # 成都主城区，可改成 beijing[0] 或 chengdu 里的其他代码
}

# 发送 GET 请求获取天气数据，超时 15 秒防止长时间挂起
response = requests.get(url, headers=headers, params=params, timeout=15)
response.raise_for_status()  # HTTP 状态码非 2xx 时抛出异常，快速暴露问题

# 打印响应状态码和正文，方便人工核对返回的 JSON 结构
print(f"状态码: {response.status_code}")
print("响应内容:")
print(response.text)
