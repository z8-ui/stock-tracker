# -*- coding: utf-8 -*-
"""数据层测试：secid 拼接 / 估值口径 / 内在价值 / 缓存时间戳（全部离线）

运行：python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_service as d  # noqa: E402


class TestNum(unittest.TestCase):
    """东财缺值 "-" 不能炸掉整批数据。"""

    def test_dash_and_blank(self):
        self.assertEqual(d._num("-"), 0.0)
        self.assertEqual(d._num("--"), 0.0)
        self.assertEqual(d._num(None), 0.0)
        self.assertEqual(d._num(""), 0.0)
        self.assertEqual(d._num("1.5"), 1.5)
        self.assertEqual(d._num("bad", 7.0), 7.0)


class TestSectorValuationParse(unittest.TestCase):
    def test_dash_pb_does_not_kill_batch(self):
        payload = {"data": {"diff": [
            {"f14": "视频媒体", "f9": 153.49, "f23": "-", "f3": 6.25, "f20": 65157205000},
            {"f14": "银行", "f9": 6.1, "f23": 0.7, "f3": 0.5, "f20": 1e12},
        ]}}
        with mock.patch.object(d, "_get", return_value=json.dumps(payload)):
            rows = d.get_sector_valuation()
        self.assertEqual(len(rows), 2)          # 以前整个循环会因 float("-") 抛异常
        self.assertEqual(rows[0]["pb"], 0.0)
        self.assertEqual(rows[1]["pb"], 0.7)

    def test_loss_sector_skipped(self):
        payload = {"data": {"diff": [{"f14": "亏损板块", "f9": -12.0, "f23": 2.0,
                                      "f3": 1.0, "f20": 1e10}]}}
        with mock.patch.object(d, "_get", return_value=json.dumps(payload)):
            self.assertIsNone(d.get_sector_valuation())


class TestSecid(unittest.TestCase):
    def test_market_prefix(self):
        self.assertEqual(d._fflow_secid("600519"), "1.600519")   # 沪市
        self.assertEqual(d._fflow_secid("900901"), "1.900901")   # 沪B
        self.assertEqual(d._fflow_secid("300750"), "0.300750")   # 深市创业板
        self.assertEqual(d._fflow_secid("002842"), "0.002842")   # 深市主板
        self.assertEqual(d._fflow_secid("830799"), "0.830799")   # 北交所


class _Stubs:
    """把网络依赖全部替换掉的上下文管理器。"""

    def __init__(self, pe, price=100.0, industry_pe=25.0, fin=None):
        self.quote = {"code": "600519", "name": "测试股", "price": price, "pe": pe,
                      "pb": 5.0, "turnover": 10.0}
        self.ind = {"pe": industry_pe, "industry": "白酒", "source": "eastmoney"}
        self.fin = fin
        self._stack = None

    def __enter__(self):
        self._stack = mock.patch.multiple(
            d,
            get_stock_quote=mock.Mock(return_value=self.quote),
            get_industry_pe=mock.Mock(return_value=self.ind),
            get_latest_financials=mock.Mock(return_value=self.fin),
            get_policy_notes=mock.Mock(return_value=[]),
        )
        self._stack.start()
        return self

    def __exit__(self, *exc):
        self._stack.stop()
        return False


class TestStockValuation(unittest.TestCase):
    def test_pe_missing_returns_null_not_industry_pe(self):
        with _Stubs(pe=0):
            v = d.get_stock_valuation("600519")
        self.assertIsNone(v["pe"])
        self.assertIsNone(v["pe_ttm"])
        self.assertIsNone(v["deviation_pe"])
        self.assertEqual(v["level"], "数据不可用")
        self.assertFalse(v["valuation_available"])
        self.assertIn("PE 缺失", v["level_reason"])

    def test_pe_normal(self):
        with _Stubs(pe=31.0):        # (31-25)/25 = +24%
            v = d.get_stock_valuation("600519")
        self.assertAlmostEqual(v["deviation_pe"], 24.0, places=1)
        self.assertEqual(v["level"], "偏高")
        self.assertTrue(v["valuation_available"])

    def test_pe_boundary_20pct_is_fair(self):
        # 记录现状：偏离正好 +20% 算"合理"（原实现严格大于 20 才算偏高）
        with _Stubs(pe=30.0):
            v = d.get_stock_valuation("600519")
        self.assertAlmostEqual(v["deviation_pe"], 20.0, places=1)
        self.assertEqual(v["level"], "合理")

    def test_industry_pe_missing(self):
        with _Stubs(pe=30.0, industry_pe=0):
            v = d.get_stock_valuation("600519")
        self.assertIsNone(v["deviation_pe"])
        self.assertEqual(v["level"], "数据不可用")
        self.assertIn("行业 PE", v["level_reason"])

    def test_pe_ttm_note_present(self):
        with _Stubs(pe=30.0):
            v = d.get_stock_valuation("600519")
        self.assertIn("同源", v["pe_ttm_note"])


class TestIntrinsicValue(unittest.TestCase):
    def test_quarterly_eps_annualized(self):
        # 三季报 EPS 0.9 → 年化 1.2 → 内在价值 1.2 × 25 × 0.8 = 24
        with _Stubs(pe=30.0, price=100.0, fin={"eps": 0.9, "report_date": "2026-09-30"}):
            iv = d.estimate_intrinsic_value(d.get_stock_quote("600519"))
        self.assertTrue(iv["available"])
        self.assertAlmostEqual(iv["eps"], 1.2, places=3)
        self.assertAlmostEqual(iv["intrinsic_value"], 24.0, places=2)
        self.assertEqual(iv["eps_source"], "quarterly_report")

    def test_no_eps_no_pe_returns_unavailable(self):
        with _Stubs(pe=0, price=100.0, fin=None):
            iv = d.estimate_intrinsic_value(d.get_stock_quote("600519"))
        self.assertFalse(iv["available"])
        self.assertIsNone(iv["intrinsic_value"])   # 以前这里会是 price=100（自证合理）
        self.assertIsNone(iv["gap"])

    def test_negative_eps_rejected(self):
        with _Stubs(pe=0, price=100.0, fin={"eps": -0.5, "report_date": "2026-09-30"}):
            iv = d.estimate_intrinsic_value(d.get_stock_quote("600519"))
        self.assertFalse(iv["available"])
        self.assertEqual(iv["eps_source"], "report_nonpositive")
        self.assertIn("亏损", iv["reason"])

    def test_price_pe_derived_marked(self):
        with _Stubs(pe=30.0, price=120.0, fin=None):
            iv = d.estimate_intrinsic_value(d.get_stock_quote("600519"))
        self.assertEqual(iv["eps_source"], "price_pe_derived")
        self.assertAlmostEqual(iv["eps"], 4.0, places=3)


class TestCacheFetchedAt(unittest.TestCase):
    """重启后必须能显示真实抓取时间，而不是把旧数据标成'刚刚获取'。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.remove(self.tmp.name)          # 让缓存文件一开始不存在
        self._orig_cache_file = d._CACHE_FILE
        d._CACHE_FILE = self.tmp.name
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()

    def tearDown(self):
        d._CACHE_FILE = self._orig_cache_file
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_fetched_at_persisted_and_reused(self):
        calls = {"n": 0}

        def fetcher():
            calls["n"] += 1
            return {"value": 42}

        with mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            first = d._cached_get("k1", fetcher, source="test", ttl=60)
            self.assertEqual(calls["n"], 1)
            self.assertIsNotNone(first["_asof"])

            # 模拟重启：内存缓存与 _LAST_SUCCESS 全清空，只剩磁盘
            d._IN_MEMORY_CACHE.clear()
            d._LAST_SUCCESS.clear()
            second = d._cached_get("k1", fetcher, source="test", ttl=60)

        self.assertEqual(calls["n"], 1)                      # 命中磁盘缓存，没重新抓
        self.assertEqual(second["value"], 42)
        self.assertEqual(second["_asof"], first["_asof"])    # 时间来自落盘，不是 now
        self.assertTrue(second["_asof_known"])

    def test_on_disk_format(self):
        with mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            d._cached_get("k2", lambda: {"v": 1}, ttl=60)
        with open(self.tmp.name, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertIn("__data", raw["k2"])
        self.assertIn("fetched_at", raw["k2"])

    def test_legacy_entry_reports_unknown_time(self):
        # 旧格式：直接是数据本体，没有抓取时间 → 不能猜成 now
        with open(self.tmp.name, "w", encoding="utf-8") as f:
            json.dump({"k3": {"value": 7}}, f)
        with mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            got = d._cached_get("k3", lambda: {"value": 999}, ttl=60)
        self.assertEqual(got["value"], 7)
        self.assertIsNone(got["_asof"])
        self.assertFalse(got["_asof_known"])


if __name__ == "__main__":
    unittest.main()
