"""专家 Agent 统一结果协议测试。"""

import json
import unittest

from ..contracts.expert_result import ExpertResult


class TestExpertResult(unittest.TestCase):
    """验证 A2A/MCP 文本能够被收敛成稳定结构。"""

    def test_parse_success_json(self):
        result = ExpertResult.from_text(
            json.dumps({
                "status": "success",
                "agent": "WeatherQueryAssistant",
                "data": [{"city": "成都", "temp_max": 28}],
                "message": "查询成功",
                "source": "mcp",
            }, ensure_ascii=False)
        )
        self.assertEqual(result.status, "success")
        self.assertFalse(result.unavailable)
        self.assertEqual(result.data[0]["city"], "成都")

    def test_parse_no_data_json(self):
        result = ExpertResult.from_text(
            '{"status": "no_data", "message": "未找到天气数据"}',
            agent="WeatherQueryAssistant",
        )
        self.assertEqual(result.status, "no_data")
        self.assertTrue(result.unavailable)

    def test_parse_json_code_fence(self):
        result = ExpertResult.from_text(
            '```json\n{"status": "success", "agent": "TripAssistant", "data": [{"price": 88}]}\n```'
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.data[0]["price"], 88)

    def test_parse_database_error_text(self):
        result = ExpertResult.from_text(
            "MySQL Connection not available",
            agent="TicketAssistant",
        )
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "data_source_unavailable")

    def test_parse_input_required_text(self):
        result = ExpertResult.from_text(
            "请提供城市和日期范围。",
            agent="WeatherQueryAssistant",
        )
        self.assertEqual(result.status, "input_required")
        self.assertTrue(result.unavailable)


if __name__ == "__main__":
    unittest.main()
