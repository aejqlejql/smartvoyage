"""票务 A2A 集成测试，仅验证独立服务之间的 HTTP 调用。"""

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
class TestTicketServer(unittest.TestCase):
    """验证票务 A2A 服务的真实 HTTP 调用链。"""

    def test_query(self):
        """检查 Agent Card 可访问后，通过 A2AClient 发送票务查询。"""
        url = os.getenv("TICKET_A2A_URL", "http://127.0.0.1:5006").rstrip("/")
        try:
            # 只有服务在线时才发送业务请求，避免依赖未启动导致测试误报。
            requests.get(f"{url}/.well-known/agent.json", timeout=3).raise_for_status()
        except requests.RequestException as exc:
            self.skipTest(f"ticket A2A service is unavailable: {exc}")

        result = A2AClient(url).ask("帮我查询北京到成都，5月1号的火车票")
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
