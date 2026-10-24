"""日期标准化和无数据守门规则测试。"""

import unittest

from ..core.guardrails import (
    all_results_unavailable,
    contains_data_source_error,
    contains_explicit_no_data_signal,
    contains_no_data_signal,
    normalize_query_payload,
    normalize_yearless_dates,
)


class TestGuardrails(unittest.TestCase):
    """验证业务准确性相关的确定性规则。"""

    def test_normalize_yearless_month_day(self):
        result = normalize_yearless_dates("北京5月1日的天气", default_year=2026)
        self.assertEqual(result, "北京2026-05-01的天气")

    def test_keep_explicit_year(self):
        result = normalize_yearless_dates("北京2026年5月1日的天气", default_year=2026)
        self.assertEqual(result, "北京2026年5月1日的天气")

    def test_normalize_nested_queries(self):
        result = normalize_query_payload(
            {"weather": "北京5月1日", "items": ["成都5月2号"]},
            default_year=2026,
        )
        self.assertEqual(result, {
            "weather": "北京2026-05-01",
            "items": ["成都2026-05-02"],
        })

    def test_unavailable_markers_are_detected(self):
        self.assertTrue(contains_no_data_signal("MySQL Connection not available"))
        self.assertTrue(contains_no_data_signal("天气查询服务暂时不可用"))
        self.assertFalse(contains_no_data_signal("北京2026-05-01：晴，18~27℃"))

    def test_no_data_and_source_errors_are_distinct(self):
        self.assertTrue(contains_explicit_no_data_signal("未找到天气数据，请确认城市和日期。"))
        self.assertFalse(contains_data_source_error("未找到天气数据，请确认城市和日期。"))
        self.assertTrue(contains_data_source_error("MySQL Connection not available"))

    def test_all_unavailable_collaboration_results(self):
        results = [
            {"result": "天气查询服务暂时不可用，请稍后再试。"},
            {"result": '{"status": "error", "code": "database_unavailable"}'},
        ]
        self.assertTrue(all_results_unavailable(results))

    def test_all_unavailable_structured_results(self):
        results = [
            {"status": "no_data", "result": "未找到天气数据"},
            {"status": "error", "result": "票务数据源暂时不可用"},
        ]
        self.assertTrue(all_results_unavailable(results))

    def test_structured_success_result_is_available(self):
        results = [
            {"status": "no_data", "result": "未找到天气数据"},
            {"status": "success", "data": [{"price": 796}], "result": "有票"},
        ]
        self.assertFalse(all_results_unavailable(results))

    def test_mixed_collaboration_results_are_not_all_unavailable(self):
        results = [
            {"result": "天气查询服务暂时不可用，请稍后再试。"},
            {"result": "北京到上海 2026-05-01 有一条已确认的车次记录。"},
        ]
        self.assertFalse(all_results_unavailable(results))


if __name__ == "__main__":
    unittest.main()
