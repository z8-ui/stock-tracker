# -*- coding: utf-8 -*-
"""S3-3A：板块 Provider（分页 / 缺失语义 / 状态维度）专项测试。

全部离线：`_get()` 被替换成按页码发报文的假实现，不发真实网络请求。

覆盖对应关系（计划 §6.1/§6.2 + D5 契约）：
  P01-P14  资金流：夹具真实报文、dict 形态、多页合并、单页失败、data=null、
           空页、rc≠0、全网络失败、"-"、不可解析、无名称、重复、pz 上限、total 不一致
  V01-V10  估值：正/负/零/null PE、PB="-"、市值缺失、空、全量 496 计数
  B01-B07  ProviderResult 契约向后兼容 + 三维度
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.data.base import Provider, ProviderResult                      # noqa: E402
from services.data.exceptions import (                                       # noqa: E402
    ProviderFormatError, ProviderNetworkError,
)
from services.data.providers.eastmoney_sector import (                       # noqa: E402
    EastmoneySectorProvider, num, PAGE_SIZE_MAX,
)

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "s3_0")


def row(code, name, f3=1.0, f9=20.0, f23="-", f20=1e10, f62=None):
    r = {"f12": code, "f14": name, "f2": 1000.0, "f3": f3, "f9": f9,
         "f23": f23, "f20": f20}
    if f62 is not None:
        r["f62"] = f62
    return r


def payload(rows, total=None):
    return {"rc": 0, "data": {"total": total if total is not None else len(rows),
                              "diff": rows}}


class FakeSector(EastmoneySectorProvider):
    """把 `_get()` 换成脚本化的响应：{pn: payload | Exception | None}。"""

    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self.calls = []

    def _get(self, params):
        pn = int(params["pn"])
        self.calls.append(params)
        v = self.pages.get(pn)
        if isinstance(v, Exception):
            raise v
        if v is None:                     # 越界页：data=null
            return {"rc": 0, "data": None}
        return v


class TestFlowParse(unittest.TestCase):
    def test_P01_real_fixture_single_page_is_partial(self):
        """真实夹具：一页 60 条，但 total=496 → 必须判 partial，不能当 success。"""
        with open(os.path.join(FIXTURE, "em_sector_flow.raw"), encoding="utf-8") as f:
            raw = json.load(f)
        p = FakeSector({1: raw, 2: None})
        res = p.fetch(mode="flow")
        self.assertEqual(res.status, "partial")
        self.assertEqual(len(res.data), 60)
        self.assertEqual(res.extra["total"], 496)
        self.assertEqual(res.extra["fetched"], 60)
        self.assertTrue(any("rows_mismatch" in m for m in res.extra["missing"]))
        first = res.data[0]
        self.assertEqual(sorted(first), ["change", "flow", "name"])
        src = raw["data"]["diff"][0]
        self.assertAlmostEqual(first["flow"], round(src["f62"] / 1e8, 2), places=2)

    def test_P02_diff_as_dict(self):
        p = FakeSector({1: {"rc": 0, "data": {"total": 2, "diff": {
            "0": row("BK0001", "甲", f62=1e8), "1": row("BK0002", "乙", f62=-1e8)}}}})
        res = p.fetch(mode="flow")
        self.assertEqual(res.status, "success")
        self.assertEqual([r["name"] for r in res.data], ["甲", "乙"])

    def test_P03_three_pages_merge(self):
        pages = {
            1: payload([row(f"BK{i:04d}", f"板块{i}") for i in range(100)], total=250),
            2: payload([row(f"BK{i:04d}", f"板块{i}") for i in range(100, 200)], total=250),
            3: payload([row(f"BK{i:04d}", f"板块{i}") for i in range(200, 250)], total=250),
            4: None,
        }
        p = FakeSector(pages)
        res = p.fetch(mode="flow")
        self.assertEqual(res.status, "success")
        self.assertEqual(len(res.data), 250)
        self.assertEqual(res.extra["completeness"], "complete")
        self.assertEqual(res.extra["missing_pages"], [])
        self.assertEqual(p.calls[-1]["pz"], str(PAGE_SIZE_MAX))

    def test_P04_page2_failure_is_partial(self):
        pages = {
            1: payload([row(f"BK{i:04d}", f"板块{i}") for i in range(100)], total=250),
            2: ProviderNetworkError("eastmoney_sector", "boom"),
            3: payload([row(f"BK{i:04d}", f"x{i}") for i in range(200, 250)], total=250),
        }
        res = FakeSector(pages).fetch(mode="flow")
        self.assertEqual(res.status, "partial")
        self.assertIn(2, res.extra["missing_pages"])
        self.assertEqual(len(res.data), 150)          # 已抓到的保留

    def test_P05_page2_data_null_stops(self):
        pages = {1: payload([row(f"BK{i:04d}", f"板块{i}") for i in range(100)], total=250),
                 2: None}
        res = FakeSector(pages).fetch(mode="flow")
        self.assertEqual(res.extra["stopped"], "data_null")
        self.assertEqual(res.status, "partial")
        self.assertEqual(len(res.data), 100)

    def test_P06_first_page_data_null_is_empty(self):
        res = FakeSector({1: None}).fetch(mode="flow")
        self.assertEqual(res.status, "empty")
        self.assertEqual(res.data, [])

    def test_P07_rc_nonzero_is_failed(self):
        res = FakeSector({1: {"rc": 1, "data": None}}).fetch(mode="flow")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderFormatError")

    def test_P08_all_network_failures_is_failed(self):
        res = FakeSector({pn: ProviderNetworkError("eastmoney_sector", "down")
                          for pn in range(1, 21)}).fetch(mode="flow")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderNetworkError")
        self.assertIsNone(res.data)

    def test_P09_dash_flow_is_none_not_zero(self):
        res = FakeSector({1: payload([row("BK0001", "甲", f62="-")], total=1)}).fetch(mode="flow")
        self.assertEqual(res.status, "success")
        self.assertIsNone(res.data[0]["flow"])

    def test_P10_unparsable_change_is_format_error(self):
        res = FakeSector({1: payload([row("BK0001", "甲", f3="abc")], total=1)}).fetch(mode="flow")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderFormatError")

    def test_P11_missing_name_skipped_and_noted(self):
        res = FakeSector({1: payload([{"f12": "BK0009", "f3": 1.0, "f62": 1e8},
                                      row("BK0001", "甲", f62=1e8)], total=2)}).fetch(mode="flow")
        self.assertEqual(len(res.data), 1)
        self.assertIn("skipped_no_name:1", res.notes)
        self.assertEqual(res.status, "partial")       # 1 != total

    def test_P12_duplicates_deduped_and_flagged(self):
        r = row("BK0001", "甲", f62=1e8)
        res = FakeSector({1: payload([r], total=2), 2: payload([dict(r)], total=2),
                          3: None}).fetch(mode="flow")
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.extra["duplicates"], 1)
        self.assertEqual(res.status, "partial")

    def test_P13_page_size_is_clamped(self):
        p = FakeSector({1: payload([row("BK0001", "甲", f62=1e8)], total=1)})
        p.fetch(mode="flow", page_size=500)
        self.assertEqual(p.calls[0]["pz"], "100")

    def test_P14_total_mismatch_across_pages(self):
        pages = {1: payload([row(f"BK{i:04d}", f"a{i}") for i in range(100)], total=200),
                 2: payload([row(f"BK{i:04d}", f"b{i}") for i in range(100, 200)], total=201),
                 3: None}
        res = FakeSector(pages).fetch(mode="flow")
        self.assertEqual(res.status, "partial")
        self.assertTrue(any("total_mismatch" in m for m in res.extra["missing"]))


class TestValuationParse(unittest.TestCase):
    def test_V01_normal(self):
        res = FakeSector({1: payload([row("BK0001", "银行", f9=6.1, f23=0.7,
                                          f3=0.5, f20=1e12)], total=1)}).fetch(mode="valuation")
        self.assertEqual(res.status, "success")
        self.assertEqual(res.data[0], {"name": "银行", "pe": 6.1, "pb": 0.7,
                                       "change": 0.5, "market_cap": 10000.0})

    def test_V02_dash_pb_is_none_not_zero(self):
        with open(os.path.join(FIXTURE, "em_sector_valuation.raw"), encoding="utf-8") as f:
            raw = json.load(f)
        res = FakeSector({1: raw, 2: None}).fetch(mode="valuation")
        self.assertTrue(all(r["pb"] is None for r in res.data))
        self.assertIn("pb_unavailable:60", res.notes)

    def test_V03_negative_pe_kept(self):
        res = FakeSector({1: payload([row("BK0001", "亏损", f9=-102.9)], total=1)}).fetch(mode="valuation")
        self.assertEqual(res.data[0]["pe"], -102.9)
        self.assertIn("negative_pe:1", res.notes)

    def test_V04_zero_pe_kept_and_not_counted_negative(self):
        res = FakeSector({1: payload([row("BK0001", "零", f9=0)], total=1)}).fetch(mode="valuation")
        self.assertEqual(res.data[0]["pe"], 0.0)
        self.assertNotIn("negative_pe:1", res.notes)

    def test_V05_dash_pe_is_none(self):
        res = FakeSector({1: payload([row("BK0001", "无PE", f9="-")], total=1)}).fetch(mode="valuation")
        self.assertIsNone(res.data[0]["pe"])

    def test_V06_unparsable_pe_fails(self):
        res = FakeSector({1: payload([row("BK0001", "坏", f9="abc")], total=1)}).fetch(mode="valuation")
        self.assertEqual(res.status, "failed")

    def test_V07_missing_market_cap_is_none(self):
        r = row("BK0001", "甲")
        r.pop("f20")
        res = FakeSector({1: payload([r], total=1)}).fetch(mode="valuation")
        self.assertIsNone(res.data[0]["market_cap"])

    def test_V08_empty_list_is_empty(self):
        res = FakeSector({1: payload([], total=0)}).fetch(mode="valuation")
        self.assertEqual(res.status, "empty")
        self.assertEqual(res.data, [])

    def test_V09_full_496_counts(self):
        """合成 496 行（90 负 PE、PB 全 "-"）：行数、计数、完整性都要对。"""
        rows = []
        for i in range(496):
            pe = -100.0 - i if i < 90 else 20.0 + i
            rows.append(row(f"BK{i:04d}", f"板块{i}", f9=pe, f23="-", f20=1e10))
        pages = {1: payload(rows[:100], total=496), 2: payload(rows[100:200], total=496),
                 3: payload(rows[200:300], total=496), 4: payload(rows[300:400], total=496),
                 5: payload(rows[400:], total=496), 6: None}
        res = FakeSector(pages).fetch(mode="valuation")
        self.assertEqual(res.status, "success")
        self.assertEqual(len(res.data), 496)
        self.assertIn("negative_pe:90", res.notes)
        self.assertIn("pb_unavailable:496", res.notes)
        self.assertEqual(sum(1 for r in res.data if r["pe"] < 0), 90)
        self.assertTrue(all(r["pb"] is None for r in res.data))

    def test_V10_num_rules(self):
        self.assertIsNone(num("-", "f23"))
        self.assertIsNone(num(None, "f23"))
        self.assertIsNone(num("", "f23"))
        self.assertEqual(num("0", "f23"), 0.0)
        self.assertEqual(num(0, "f23"), 0.0)
        self.assertEqual(num("1,234.5", "f23"), 1234.5)
        with self.assertRaises(ProviderFormatError):
            num("abc", "f23")


class _OldStyle(Provider):
    """模拟"还没迁移"的 Provider：normalize 只返回 2 元组。"""

    name = "old_style"

    def request(self, *a, **k):
        return "raw"

    def normalize(self, raw, *a, **k):
        return [{"x": 1}], "2026-11-10 15:00:00"


class _PartialProvider(Provider):
    name = "partial_style"

    def request(self, *a, **k):
        return "raw"

    def normalize(self, raw, *a, **k):
        return [{"x": 1}], "", {"missing_pages": [2], "notes": ["n1"],
                                "completeness": "partial"}


class TestProviderResultContract(unittest.TestCase):
    def test_B01_old_positional_construction_still_works(self):
        r = ProviderResult(None, "p", "failed", "", 0.0, False, {"m": 1}, 1.0)
        self.assertEqual((r.data, r.source, r.status), (None, "p", "failed"))
        self.assertEqual((r.missing, r.notes, r.extra), ([], [], {}))

    def test_B02_as_dict_keeps_old_keys_and_adds_new(self):
        d = ProviderResult([1], "p", "success").as_dict()
        for k in ("data", "source", "status", "asof", "fetched_at", "is_stale",
                  "error", "elapsed_ms", "attempts", "ok"):
            self.assertIn(k, d)
        for k in ("missing", "notes", "extra"):
            self.assertIn(k, d)

    def test_B03_two_tuple_normalize_is_success(self):
        res = _OldStyle().fetch()
        self.assertEqual(res.status, "success")
        self.assertEqual(res.asof, "2026-11-10 15:00:00")
        self.assertEqual(res.extra, {})

    def test_B04_three_tuple_missing_pages_is_partial(self):
        res = _PartialProvider().fetch()
        self.assertEqual(res.status, "partial")
        self.assertEqual(res.missing, [2])
        self.assertEqual(res.notes, ["n1"])
        self.assertEqual(res.dimensions()["completeness"], "partial")

    def test_B05_empty_is_empty_not_failed(self):
        res = FakeSector({1: payload([], total=0)}).fetch(mode="flow")
        self.assertEqual(res.status, "empty")
        self.assertEqual(res.dimensions()["fetch_status"], "empty")

    def test_B06_dimensions_three_axes(self):
        ok = ProviderResult([1], "p", "success")
        self.assertEqual(ok.dimensions(), {"fetch_status": "success",
                                           "completeness": "complete",
                                           "freshness": "fresh"})
        bad = ProviderResult(None, "p", "failed")
        self.assertEqual(bad.dimensions(), {"fetch_status": "failed",
                                            "completeness": "unknown",
                                            "freshness": "unknown"})
        stale = ProviderResult([1], "p", "success", is_stale=True)
        self.assertEqual(stale.dimensions()["freshness"], "stale")

    def test_B07_mutable_defaults_are_not_shared(self):
        a, b = ProviderResult(), ProviderResult()
        a.missing.append("x")
        a.extra["k"] = 1
        self.assertEqual(b.missing, [])
        self.assertEqual(b.extra, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
