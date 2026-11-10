# -*- coding: utf-8 -*-
"""数据层测试：secid 拼接 / 估值口径 / 内在价值 / 缓存时间戳（全部离线）

运行：python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_service as d  # noqa: E402
from services.data.providers.eastmoney_sector import EastmoneySectorProvider  # noqa: E402


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
    """板块估值解析（S3-3A 起走 EastmoneySectorProvider）。"""

    class _Offline:
        """离线上下文：报文由 `_get` 提供，缓存不落盘、不串味。"""

        def __init__(self, rows, total=None):
            self.payload = {"rc": 0, "data": {
                "total": total if total is not None else len(rows), "diff": rows}}
            self._stack = None

        def __enter__(self):
            self._stack = ExitStack()
            self._stack.enter_context(
                mock.patch.object(EastmoneySectorProvider, "_get",
                                  return_value=self.payload))
            self._stack.enter_context(mock.patch.object(d, "_IN_MEMORY_CACHE", {}))
            self._stack.enter_context(mock.patch.object(d, "_load_cache", lambda: {}))
            self._stack.enter_context(mock.patch.object(d, "_save_cache", lambda c: None))
            self._stack.enter_context(mock.patch.object(d, "_LAST_SUCCESS", {}))
            return self

        def __exit__(self, *exc):
            self._stack.close()
            return False

    def test_dash_pb_is_none_and_real_pb_kept(self):
        """D3 口径：「-」是缺失 → null；真实 0.7 仍然是 0.7。

        原测试 `test_dash_pb_does_not_kill_batch` 断言 `rows[0]["pb"] == 0.0`，
        那是 `_num("-", default=0.0)` 的旧行为；D3 明确禁止把缺失写成 0 倍。
        测试价值保留：仍然验证"一条缺失 PB + 一条真实 PB"不会让整批解析崩掉。
        """
        rows = [{"f12": "BK0001", "f14": "视频媒体", "f9": 153.49, "f23": "-",
                 "f3": 6.25, "f20": 65157205000},
                {"f12": "BK0002", "f14": "银行", "f9": 6.1, "f23": 0.7,
                 "f3": 0.5, "f20": 1e12}]
        with self._Offline(rows):
            out = d.get_sector_valuation()
        self.assertEqual(len(out), 2)              # 以前整个循环会因 float("-") 抛异常
        self.assertIsNone(out[0]["pb"])            # 缺失 ≠ 0 倍
        self.assertEqual(out[1]["pb"], 0.7)        # 真实值不受影响

    def test_loss_sector_retained(self):
        """D1（S3-3 授权定向改写）：负 PE 板块不再被丢弃。

        原测试 `test_loss_sector_skipped` 断言"负 PE 被过滤掉、函数返回 None"，
        那是 S3-3 之前的旧口径；D4 明确要求 496 个板块全部返回、负 PE 保留原值，
        亏损由前端单独标注，不允许用过滤或 abs() 伪造排名。
        测试价值保留：仍然验证"一条负 PE 记录"的完整链路，只是结论从"被丢弃"变成"被保留"。
        """
        rows = [{"f12": "BK0001", "f14": "亏损板块", "f9": -12.0, "f23": 2.0,
                 "f3": 1.0, "f20": 1e10}]
        with self._Offline(rows):
            out = d.get_sector_valuation()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["pe"], -12.0)
        self.assertEqual(out[0]["name"], "亏损板块")


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

    def test_price_pe_derived_marked_as_reference_only(self):
        """反推 EPS 只能作为「相对PE参考」，不得进入内在价值计算。

        以前 fin 缺失就 EPS = price/PE，再乘行业PE×0.8 —— 结果恒等于
        price×0.8×(个股PE/行业PE) 的变形，等于用股价自证估值。
        """
        with _Stubs(pe=30.0, price=120.0, fin=None):
            iv = d.estimate_intrinsic_value(d.get_stock_quote("600519"))
        self.assertFalse(iv["available"])
        self.assertIsNone(iv["intrinsic_value"])
        self.assertIsNone(iv["eps"])
        self.assertEqual(iv["eps_source"], "report_missing")
        ref = iv["pe_reference"]
        self.assertTrue(ref["available"])
        self.assertAlmostEqual(ref["eps_derived"], 4.0, places=3)
        self.assertEqual(ref["label"].split("（")[0], "相对PE参考")
        self.assertIn("非财报口径", ref["label"])

    def test_unknown_industry_pe_is_not_25x(self):
        """行业 PE 取不到时必须是 None，不能再默认 25 倍。"""
        with _Stubs(pe=12.0, price=50.0, fin={"eps": 1.0, "report_date": "2025-12-31"}):
            d.get_industry_pe = lambda code: {"pe": None, "industry": "未知",
                                              "source": "unavailable",
                                              "source_label": "不可用", "is_static": False}
            iv = d.estimate_intrinsic_value(d.get_stock_quote("600519"))
        self.assertFalse(iv["available"])
        self.assertIsNone(iv["intrinsic_value"])
        self.assertIn("行业 PE", iv["reason"])


class TestIndustryPeFallback(unittest.TestCase):
    """静态行业 PE 的口径：表里有就用且标记 is_static，没有就是 None。"""

    def test_unknown_code_returns_none_not_25(self):
        with mock.patch.object(d, "_cached_get", return_value=None):
            ind = d.get_industry_pe("999999")
        self.assertIsNone(ind["pe"])
        self.assertEqual(ind["source"], "unavailable")
        self.assertFalse(ind["is_static"])

    def test_static_map_hit_is_labelled(self):
        self.assertIn("600519", d.INDUSTRY_PE_MAP)
        with mock.patch.object(d, "_cached_get", return_value=None):
            ind = d.get_industry_pe("600519")
        self.assertEqual(ind["pe"], d.INDUSTRY_PE_MAP["600519"])
        self.assertTrue(ind["is_static"])
        self.assertIn("静态", ind["source_label"])

    def test_live_result_is_not_static(self):
        live = {"pe": 20.0, "industry": "白酒Ⅱ", "source": "eastmoney",
                "source_label": "东方财富行业板块动态 PE（实时接口）", "is_static": False}
        with mock.patch.object(d, "_cached_get", return_value=live):
            ind = d.get_industry_pe("600519")
        self.assertFalse(ind["is_static"])
        self.assertEqual(ind["pe"], 20.0)


class TestValuationModelParams(unittest.TestCase):
    def test_safety_margin_is_explicit_parameter(self):
        import config
        with _Stubs(pe=20.0, price=30.0, fin={"eps": 1.5, "report_date": "2025-12-31"}):
            iv = d.estimate_intrinsic_value(d.get_stock_quote("600519"))
        self.assertEqual(iv["model_params"]["safety_margin"], config.VALUATION_SAFETY_MARGIN)
        self.assertAlmostEqual(iv["intrinsic_value"],
                               1.5 * 25 * config.VALUATION_SAFETY_MARGIN, places=2)


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
            # 重抓也失败（返回 None）→ 只能退回旧缓存，并诚实标记"时间未知+陈旧"
            got = d._cached_get("k3", lambda: None, ttl=60)
        self.assertEqual(got["value"], 7)
        self.assertIsNone(got["_asof"])
        self.assertFalse(got["_asof_known"])
        self.assertTrue(got["_stale"])


if __name__ == "__main__":
    unittest.main()
