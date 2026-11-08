# -*- coding: utf-8 -*-
"""Hotfix 回归：市场资金字段口径（f62/f66/f72/f78/f84 为金额，f69/f75 为占比）

背景：真实报文核对发现 get_market_flow() 把 f69（超大单占比）当"中单"、
f75（大单占比）当"小单"，且 val2yi() 按数值大小猜单位 —— 于是页面上的中单/小单
是百分比被当成"亿元"。10 个用例锁死修复后的口径，另加真实报文回放与恒等式守恒。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import data_service as d          # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")


class Resp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def json(self):
        return json.loads(self.text)


def tencent(price="3649.18", when="20261106150000", change="-1.10"):
    parts = [""] * 88
    parts[0], parts[1], parts[2], parts[3] = "1", "上证指数", "000001", price
    parts[30], parts[32] = when, change
    return Resp("~".join(parts))


def em(rows):
    return Resp(json.dumps({"rc": 0, "data": {"diff": rows}}))


def one(**fields):
    """一条指数快照：默认给全 5 个金额字段，按需覆盖。"""
    row = {"f62": 5.0e8, "f66": 2.0e8, "f72": 3.0e8, "f78": 1.5e8, "f84": -6.5e8,
           "f69": -117, "f75": -145, "f81": 48, "f87": 214}
    row.update(fields)
    for k in list(row):
        if row[k] is None:
            row.pop(k)
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = d._CACHE_FILE
        self._status = d._market_status
        d._CACHE_FILE = os.path.join(self.tmp.name, "c.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        d._market_status = lambda: ("交易中", True)

    def tearDown(self):
        d._CACHE_FILE = self._cache
        d._market_status = self._status
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        self.tmp.cleanup()

    def run_flow(self, rows, tc=None):
        tc = tc or tencent()

        def fake(url, **kw):
            return tc if "gtimg" in url else em(rows)

        with mock.patch.object(d.requests, "get", side_effect=fake):
            return d.get_market_flow()


class TestFieldMapping(Base):
    def test_f69_f75_are_not_money(self):
        """1. 占比字段不得进入金额累加。"""
        out = self.run_flow([one()])
        self.assertEqual(out["middle_order"], 1.5, "中单必须来自 f78，而不是 f69(-117)")
        self.assertEqual(out["small_order"], -6.5, "小单必须来自 f84，而不是 f75(-145)")

    def test_f78_is_middle(self):
        """2. f78 → 中单净额。"""
        out = self.run_flow([one(f78=123456789)])
        self.assertEqual(out["middle_order"], 1.23)

    def test_f84_is_small(self):
        """3. f84 → 小单净额。"""
        out = self.run_flow([one(f84=-987654321)])
        self.assertEqual(out["small_order"], -9.88)

    def test_all_amounts_are_yuan_to_yi(self):
        """4. f62/f66/f72/f78/f84 一律 元→亿元、两位小数。"""
        out = self.run_flow([one(f62=1.234567e9, f66=2.345678e9, f72=3.456789e9,
                                 f78=4.567891e9, f84=5.678912e9)])
        self.assertEqual([out["main_force"], out["super_large"], out["large_order"],
                          out["middle_order"], out["small_order"]],
                         [12.35, 23.46, 34.57, 45.68, 56.79])

    def test_small_amounts_still_converted(self):
        """5. 小于 100 万元的金额也要换算（旧启发式会原样返回 600000）。"""
        out = self.run_flow([one(f62=600000, f66=600000, f72=0, f78=300000, f84=300000)])
        self.assertEqual(out["main_force"], 0.01)
        self.assertEqual(out["middle_order"], 0.0)

    def test_real_zero_kept(self):
        """6. 真实零值保留为 0.0，而不是 None。"""
        out = self.run_flow([{"f62": 0, "f66": 0, "f72": 0, "f78": 0, "f84": 0}])
        self.assertEqual([out[k] for k in ("main_force", "super_large", "large_order",
                                           "middle_order", "small_order")],
                         [0.0, 0.0, 0.0, 0.0, 0.0])
        self.assertTrue(out["_flow_available"])


class TestMissingFields(Base):
    def test_missing_middle_small_is_unknown_not_zero(self):
        """7a. 没有 f78/f84：中单/小单是"未知"，不是 0，更不能退回占比。"""
        row = {"f62": 5.0e8, "f66": 2.0e8, "f72": 3.0e8, "f69": -117, "f75": -145}
        out = self.run_flow([row])
        self.assertIsNone(out["middle_order"])
        self.assertIsNone(out["small_order"])
        self.assertTrue(out["_flow_partial"])
        self.assertNotEqual(out["middle_order"], -117, "绝不能把占比当金额")

    def test_missing_main_force_invalidates_snapshot(self):
        """7b. 主力/大单/超大单缺失 → 整份快照不可用，返回 None。"""
        row = {"f66": 2.0e8, "f72": 3.0e8, "f78": 1.0e8, "f84": 1.0e8}
        self.assertIsNone(self.run_flow([row]))

    def test_missing_field_raises_in_converter(self):
        with self.assertRaises(ValueError):
            d.yuan_to_yi(None)
        with self.assertRaises(ValueError):
            d.yuan_to_yi("")

    def test_flow_structure_unchanged(self):
        """9. 返回字段与结构不变（页面依赖）。"""
        out = self.run_flow([one()])
        for k in ("main_force", "super_large", "large_order", "middle_order",
                  "small_order", "date", "index_name", "index_price", "index_change"):
            self.assertIn(k, out)
        self.assertEqual(out["date"], "20261106")
        self.assertEqual(out["index_name"], "上证指数")


class TestAggregation(Base):
    def test_two_indexes_summed(self):
        """8. 多条指数快照按字段累加。"""
        out = self.run_flow([one(), one(f62=1.0e8, f66=5.0e7, f72=5.0e7,
                                       f78=2.0e7, f84=8.0e7)])
        self.assertEqual(out["main_force"], 6.0)
        self.assertEqual(out["super_large"], 2.5)
        self.assertEqual(out["large_order"], 3.5)
        self.assertEqual(out["middle_order"], 1.7)
        self.assertEqual(out["small_order"], -5.7)

    def test_two_decimals_only(self):
        out = self.run_flow([one(f78=1.2345678e8)])
        self.assertEqual(out["middle_order"], 1.23)
        self.assertEqual(round(out["middle_order"], 2), out["middle_order"])


class TestLegacyCacheSelfHeal(Base):
    def test_old_cache_without_marker_is_refetched(self):
        """上线后不能继续展示旧口径缓存（中单/小单是百分比），必须重取一次覆盖。"""
        import time as _t
        legacy = {"main_force": -524.97, "super_large": -233.21, "large_order": -291.77,
                  "middle_order": -279.0, "small_order": -348.0, "date": "20261106",
                  "index_name": "上证指数", "index_price": 3649.18, "index_change": -1.1}
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"market_flow": {"__data": legacy, "fetched_at": _t.time() - 600}}, f)
        out = self.run_flow([one()])
        self.assertEqual(out["middle_order"], 1.5, "旧缓存的中单(-279)不能被继续返回")
        self.assertEqual(out["_flow_fields_v"], 2)


class TestRealPayloadReplay(Base):
    """用真实采集的指数快照回放：修复前守恒差 -1151.97 亿，修复后 ≈0。"""

    def setUp(self):
        super().setUp()
        base = os.path.join(FIX, "eastmoney")
        with open(os.path.join(base, "ulist_index_2026-11-08.json"), encoding="utf-8") as f:
            self.em_text = f.read()
        with open(os.path.join(FIX, "tencent", "tencent_index_2026-11-08.txt"), encoding="utf-8") as f:
            self.tc_text = f.read()

    def test_real_snapshot_is_conserved(self):
        def fake(url, **kw):
            return Resp(self.tc_text) if "gtimg" in url else Resp(self.em_text)
        with mock.patch.object(d.requests, "get", side_effect=fake):
            out = d.get_market_flow()
        self.assertEqual(out["date"], "20261106")
        self.assertEqual(out["main_force"], -524.97)
        self.assertEqual(out["super_large"], -233.21)
        self.assertEqual(out["large_order"], -291.77)
        self.assertEqual(out["middle_order"], 45.83, "真实报文：中单 = f78 合计 45.83 亿")
        self.assertEqual(out["small_order"], 479.15, "真实报文：小单 = f84 合计 479.15 亿")
        self.assertAlmostEqual(out["main_force"] + out["middle_order"] + out["small_order"],
                               0.0, delta=0.05)
        self.assertAlmostEqual(out["super_large"] + out["large_order"],
                               out["main_force"], delta=0.05)
        self.assertFalse(out["_flow_partial"])


if __name__ == "__main__":
    unittest.main()
