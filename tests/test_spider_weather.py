"""Tests for weather crawler helpers."""

import unittest
from datetime import datetime, timedelta

from ai01_SmartVoyage.infrastructure.config import parse_weather_city_codes
from ai01_SmartVoyage.utils.spider_weather import (
    _nmc_clean,
    _wind_dir_text,
    parse_nmc_weather,
    should_update_data,
)


class TestWeatherSpiderHelpers(unittest.TestCase):
    def test_parse_default_cities(self):
        cities = parse_weather_city_codes("")
        self.assertIn("成都", cities)
        self.assertEqual(cities["成都"], "101270101")
        self.assertIn("北京", cities)
        self.assertEqual(cities["武汉"], "101200101")

    def test_parse_custom_cities(self):
        cities = parse_weather_city_codes("杭州:101210101,南京")
        self.assertEqual(cities["杭州"], "101210101")
        self.assertEqual(cities["南京"], "")

    def test_should_update_when_missing_or_forced(self):
        self.assertTrue(should_update_data(None))
        self.assertTrue(should_update_data(datetime.now(), force_update=True))

    def test_should_skip_fresh_data(self):
        latest = datetime.now() - timedelta(minutes=1)
        self.assertFalse(should_update_data(latest, min_age_minutes=10))

    def test_should_update_stale_data(self):
        latest = datetime.now() - timedelta(minutes=11)
        self.assertTrue(should_update_data(latest, min_age_minutes=10))

    def test_wind_dir_text(self):
        self.assertEqual(_wind_dir_text(0), "北风")
        self.assertEqual(_wind_dir_text(90), "东风")
        self.assertEqual(_wind_dir_text(None), "")

    def test_nmc_clean_missing(self):
        self.assertIsNone(_nmc_clean(9999))
        self.assertIsNone(_nmc_clean("9999"))
        self.assertEqual(_nmc_clean("阴"), "阴")

    def test_parse_nmc_weather(self):
        payload = {
            "msg": "success",
            "code": 0,
            "data": {
                "real": {
                    "publish_time": "2026-08-17 10:35",
                    "weather": {"humidity": 67.0, "info": "多云"},
                    "sunriseSunset": {
                        "sunrise": "2026-08-17 05:51",
                        "sunset": "2026-08-17 19:04",
                    },
                },
                "predict": {
                    "publish_time": "2026-08-17 12:00",
                    "detail": [
                        {
                            "date": "2026-08-17",
                            "day": {
                                "weather": {"info": "阴", "img": "2", "temperature": "33"},
                                "wind": {"direct": "东北风", "power": "微风"},
                            },
                            "night": {
                                "weather": {"info": "多云", "img": "1", "temperature": "26"},
                                "wind": {"direct": "东北风", "power": "微风"},
                            },
                            "precipitation": 0.0,
                        }
                    ],
                },
            },
        }
        parsed = parse_nmc_weather(payload, "武汉")
        self.assertEqual(parsed["code"], "200")
        self.assertEqual(len(parsed["daily"]), 1)
        day = parsed["daily"][0]
        self.assertEqual(day["fxDate"], "2026-08-17")
        self.assertEqual(day["textDay"], "阴")
        self.assertEqual(day["tempMax"], "33")
        self.assertEqual(day["sunrise"], "05:51")
        self.assertEqual(day["humidity"], 67.0)


if __name__ == "__main__":
    unittest.main()
