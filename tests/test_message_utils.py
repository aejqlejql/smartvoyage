"""Tests for LLM JSON parsing helpers."""

import json
import unittest

from ai01_SmartVoyage.core.message_utils import parse_llm_json


class TestParseLlmJson(unittest.TestCase):
    def test_plain_json(self):
        parsed = parse_llm_json(
            '{"intents": ["weather"], "user_queries": {"weather": "武汉天气"}, "follow_up_message": ""}'
        )
        self.assertEqual(parsed["intents"], ["weather"])
        self.assertEqual(parsed["user_queries"]["weather"], "武汉天气")

    def test_markdown_fence(self):
        parsed = parse_llm_json(
            '```json\n{"intents": ["train"], "user_queries": {"train": "高铁"}, "follow_up_message": ""}\n```'
        )
        self.assertEqual(parsed["intents"], ["train"])

    def test_chinese_comma_between_fields(self):
        raw = (
            '{"intents": ["flight", "weather", "attraction", "car_rental", "insurance"], '
            '"user_queries": {"flight": "2026-05-01从北京飞往成都的往返机票，预算3000元以内，游玩三天", '
            '"weather": "成都2026-05-01至2026-05-03天气预报", '
            '"attraction": "成都适合五一期间游览的景点推荐，预算3000元以内三日行程", '
            '"car_rental": "成都2026-05-01至2026-05-03租车服务，预算3000元以内三日行程", '
            '"insurance": "北京至成都三日旅行保险，覆盖2026-05-01至2026-05-03，预算3000元以内"}， '
            '"follow_up_message": ""}'
        )
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        parsed = parse_llm_json(raw)
        self.assertEqual(parsed["intents"][-1], "insurance")
        self.assertEqual(parsed["follow_up_message"], "")

    def test_trailing_comma(self):
        parsed = parse_llm_json('{"intents": ["weather"], "follow_up_message": "",}')
        self.assertEqual(parsed["intents"], ["weather"])

    def test_invalid_json_still_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            parse_llm_json("这不是json")


if __name__ == "__main__":
    unittest.main()
