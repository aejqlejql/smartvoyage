"""行程 A2A 集成测试，仅验证独立服务之间的 HTTP 调用。"""

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
class TestTripServer(unittest.TestCase):
    """验证行程 A2A 服务的真实 HTTP 调用链。"""

    def test_query(self):
        """检查 Agent Card 可访问后，通过 A2AClient 发送租车查询。"""
        url = os.getenv("TRIP_A2A_URL", "http://127.0.0.1:5007").rstrip("/")
        try:
            # 先探测 Agent Card，避免没有启动行程服务时直接连接失败。
            requests.get(f"{url}/.well-known/agent.json", timeout=3).raise_for_status()
        except requests.RequestException as exc:
            self.skipTest(f"trip A2A service is unavailable: {exc}")

        result = A2AClient(url).ask("帮我查询成都5月1号的租车信息，成都取车，成都还车")
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
