# -*- coding: utf-8 -*-
"""交易日历测试（2026-11 数据真实性）

没有日历文件时只能按周末判断，这一点必须显式暴露（configured=False），
不能让人以为"没报错就是对的"。
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import market_calendar as mc        # noqa: E402


class TestCalendar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "market_calendar.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, obj):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_missing_file_is_not_configured(self):
        cal = mc.load_calendar(os.path.join(self.tmp.name, "nope.json"))
        self.assertFalse(cal["configured"])
        self.assertIn("未配置", cal["error"])
        ok, reason = mc.is_trading_day(date(2026, 10, 1), cal)
        self.assertTrue(ok, "没有日历时只能退回常规判断，这一点由 configured=False 暴露")
        self.assertEqual(reason, "工作日")

    def test_holiday_blocks_weekday(self):
        self._write({"holidays": ["2026-10-01"], "extra_trading_days": []})
        cal = mc.load_calendar(self.path)
        self.assertTrue(cal["configured"])
        ok, reason = mc.is_trading_day(date(2026, 10, 1), cal)
        self.assertFalse(ok)
        self.assertEqual(reason, "节假日休市")

    def test_extra_trading_day_overrides_weekend(self):
        # 2026-02-14 是周六，调休补班应算交易日
        self._write({"holidays": [], "extra_trading_days": ["2026-02-14"]})
        cal = mc.load_calendar(self.path)
        ok, reason = mc.is_trading_day(date(2026, 2, 14), cal)
        self.assertTrue(ok)
        self.assertEqual(reason, "调休补班")

    def test_weekend_without_extra(self):
        cal = mc.load_calendar(self.path)
        ok, reason = mc.is_trading_day(date(2026, 2, 15), cal)
        self.assertFalse(ok)
        self.assertEqual(reason, "周末休市")

    def test_malformed_file_degrades_loudly(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        cal = mc.load_calendar(self.path)
        self.assertFalse(cal["configured"])
        self.assertIn("无法解析", cal["error"])

    def test_invalid_dates_are_skipped(self):
        self._write({"holidays": ["2026-13-40", "bad", "2026-01-01"]})
        cal = mc.load_calendar(self.path)
        self.assertEqual(len(cal["holidays"]), 1)


class TestMarketStatusIntegration(unittest.TestCase):
    def test_calendar_info_exposed(self):
        import data_service as d
        info = d.calendar_info()
        self.assertIn("configured", info)
        self.assertIn("error", info)
        self.assertIsInstance(info["holidays"], int)

    def test_bundled_calendar_matches_sse_2026(self):
        """仓库自带的上交所 2026 休市安排必须真的被读到。"""
        cal = mc.load_calendar(mc.DEFAULT_CALENDAR)
        self.assertTrue(cal["configured"], cal["error"])
        self.assertIn("上海证券交易所", cal["source"])
        for day in ("2026-01-01", "2026-02-23", "2026-10-01", "2026-10-07"):
            self.assertIn(date.fromisoformat(day), cal["holidays"], day)
        # 节后第一个工作日必须开市
        for day in ("2026-01-05", "2026-02-24", "2026-10-08"):
            ok, _ = mc.is_trading_day(date.fromisoformat(day), cal)
            self.assertTrue(ok, day)
        # 上交所通知未安排周末补班
        self.assertEqual(cal["extra"], set())

    def test_holiday_status(self):
        import data_service as d
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cal.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"holidays": [date.today().isoformat()]}, f)
            old = os.environ.get("MARKET_CALENDAR_FILE")
            os.environ["MARKET_CALENDAR_FILE"] = path
            try:
                status, trading = d._market_status()
            finally:
                if old is None:
                    os.environ.pop("MARKET_CALENDAR_FILE", None)
                else:
                    os.environ["MARKET_CALENDAR_FILE"] = old
        self.assertFalse(trading)
        self.assertEqual(status, "节假日休市")


if __name__ == "__main__":
    unittest.main()
