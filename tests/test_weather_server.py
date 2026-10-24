"""天气 A2A 集成测试。

A2A 和 MCP 必须是独立进程；设置 RUN_A2A_INTEGRATION_TESTS=1 后，
本测试才会访问已经启动的真实天气 A2A 服务。
"""

import os
import unittest

import requests

try:
    from python_a2a import A2AClient
except ModuleNotFoundError as exc:
    A2AClient = None
    A2A_IMPORT_ERROR = exc
else:
    A2A_IMPORT_ERROR = None


@unittest.skipUnless(
    os.getenv("RUN_A2A_INTEGRATION_TESTS") == "1" and A2AClient is not None,
    (
        "set RUN_A2A_INTEGRATION_TESTS=1 and install python-a2a "
        f"to run live A2A tests: {A2A_IMPORT_ERROR}"
    ),
)
class TestWeatherServer(unittest.TestCase):
    """验证天气 A2A 服务的真实 HTTP 调用链。"""

    def test_query(self):
        """检查 Agent Card 可访问后，通过 A2AClient 发送天气查询。"""
        url = os.getenv("WEATHER_A2A_URL", "http://127.0.0.1:5005").rstrip("/")
        try:
            # 先检查 Agent Card，服务未启动时跳过，而不是把环境问题当成代码失败。
            requests.get(f"{url}/.well-known/agent.json", timeout=3).raise_for_status()
        except requests.RequestException as exc:
            self.skipTest(f"weather A2A service is unavailable: {exc}")

        # A2AClient 只调用 A2A 接口，不直接导入服务端实现。
        result = A2AClient(url).ask("帮我查一下北京明天的天气")
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
