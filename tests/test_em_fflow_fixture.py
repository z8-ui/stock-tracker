# -*- coding: utf-8 -*-
"""锁死真实报文夹具与字段映射表的一致性（纯离线，不联网）

夹具来源见 tests/fixtures/eastmoney/CAPTURE.md：2026-11-08 实采 600519，
最近交易日 2026-11-06。本测试只做两件事：
  1. 夹具结构没变（列数、条数、日期、数值）；
  2. docs/V2.1_S2_EASTMONEY_FLOW_DESIGN.md 里写的映射与夹具仍然对得上。
它不证明字段语义 —— 语义的证据强度见 CAPTURE.md 的"证据强度"一列。
"""

import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "eastmoney")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


class TestDayklineFixture(unittest.TestCase):
    def setUp(self):
        self.js = load("fflow_daykline_600519_2026-11-08.json")
        self.rows = self.js["data"]["klines"]
        self.last = self.rows[-1].split(",")

    def test_response_shape(self):
        self.assertEqual(self.js["rc"], 0)
        self.assertEqual(self.js["data"]["code"], "600519")
        self.assertEqual(self.js["data"]["name"], "贵州茅台")
        self.assertEqual(self.js["data"]["market"], 1)
        self.assertEqual(len(self.rows), 120, "夹具条数变化说明报文结构变了，需重新核对")

    def test_every_row_has_six_columns_and_no_empty(self):
        counts = {len(r.split(",")) for r in self.rows}
        self.assertEqual(counts, {6})
        for r in self.rows:
            self.assertNotIn("", [p.strip() for p in r.split(",")])

    def test_last_row_is_the_verified_one(self):
        self.assertEqual(self.last[0], "2026-11-06", "真实交易日不许被改成采集日")
        self.assertEqual([float(x) for x in self.last[1:]], [
            -318894480.0, -108674.0, 319003136.0, -133286896.0, -185607584.0])

    def test_main_equals_big_plus_super_big(self):
        """f52 == f55 + f56：这是"主力=大单+超大单"的硬证据，逐行都成立。"""
        for r in self.rows:
            p = [float(x) for x in r.split(",")[1:]]
            self.assertAlmostEqual(p[0], p[3] + p[4], delta=1.0, msg=r)

    def test_main_plus_mid_plus_small_nets_to_zero(self):
        """资金守恒：主力 + 中单 + 小单 ≈ 0（因为主力=大单+超大单，不能再把 5 列相加 ——
        那等于把主力算了两遍，2026-05-14 会残留 3.8 亿）。"""
        for r in self.rows:
            p = [float(x) for x in r.split(",")[1:]]
            self.assertLess(abs(p[0] + p[1] + p[2]), 200.0, r)


class TestKlineFixture(unittest.TestCase):
    def setUp(self):
        self.js = load("fflow_kline_600519_2026-11-08.json")
        self.row = self.js["data"]["klines"][0].split(",")

    def test_shape_and_extra_trade_periods(self):
        self.assertEqual(self.js["rc"], 0)
        self.assertEqual(len(self.js["data"]["klines"]), 1)
        self.assertIn("tradePeriods", self.js["data"], "kline 比 daykline 多 tradePeriods")
        self.assertEqual(len(self.row), 5)

    def test_same_day_same_numbers_as_daykline(self):
        self.assertEqual(self.row[0], "2026-11-06")
        day = load("fflow_daykline_600519_2026-11-08.json")["data"]["klines"][-1].split(",")
        self.assertEqual(self.row[1:5], day[1:5], "同一天前 5 列必须与 daykline 完全一致")

    def test_super_big_can_be_derived(self):
        """kline 没请求 f56，只能用 主力−大单 反推，且与 daykline 的 f56 一致。"""
        day = load("fflow_daykline_600519_2026-11-08.json")["data"]["klines"][-1].split(",")
        derived = float(self.row[1]) - float(self.row[4])
        self.assertEqual(derived, float(day[5]))


class TestNamedCrossCheck(unittest.TestCase):
    """命名接口（ulist.np）是"列位置→含义"的独立锚点。"""

    def setUp(self):
        self.d = load("ulist_named_600519_2026-11-08.json")["data"]["diff"][0]
        self.day = load("fflow_daykline_600519_2026-11-08.json")["data"]["klines"][-1].split(",")

    def test_columns_match_named_fields(self):
        self.assertEqual(self.d["f62"], float(self.day[1]), "第2列 = f62 主力净额")
        self.assertEqual(self.d["f84"], float(self.day[2]), "第3列 = f84 小单")
        self.assertEqual(self.d["f78"], float(self.day[3]), "第4列 = f78 中单")
        self.assertEqual(self.d["f72"], float(self.day[4]), "第5列 = f72 大单")
        self.assertEqual(self.d["f66"], float(self.day[5]), "第6列 = f66 超大单")

    def test_main_is_big_plus_super_big_named(self):
        self.assertEqual(self.d["f62"], self.d["f72"] + self.d["f66"])

    def test_percentages_are_hundredths(self):
        """f69/f75/f81/f87 是百分比×100，四类合计≈0。"""
        total = self.d["f69"] + self.d["f75"] + self.d["f81"] + self.d["f87"]
        self.assertLess(abs(total), 10, total)


class TestFields2OrderIgnored(unittest.TestCase):
    """GPT 要求的换序验证：列顺序并不跟随 fields2，而是固定内部顺序。

    daykline 第二次请求 fields2=f51,f56,f55,f54,f53,f52，
    返回的仍是 日期,主力,小单,中单,大单,超大单 —— 与第一次逐字相同。
    kline 同样（f51,f55,f54,f53,f52 → 输出顺序不变）。
    所以"位置=请求顺序"的假设被否证；列含义只能靠数值与命名字段对齐来确定。
    """

    def test_daykline_swap_is_byte_identical(self):
        a = load("fflow_daykline_600519_2026-11-08.json")["data"]["klines"]
        b = load("fflow_daykline_swap_600519_2026-11-08.json")["data"]["klines"]
        self.assertEqual(a, b, "换序后响应变了 → 说明列顺序跟随 fields2；实测没变")

    def test_kline_swap_is_byte_identical(self):
        a = load("fflow_kline_600519_2026-11-08.json")["data"]["klines"]
        b = load("fflow_kline_swap_600519_2026-11-08.json")["data"]["klines"]
        self.assertEqual(a, b)

    def test_columns_stay_in_canonical_order(self):
        row = load("fflow_daykline_swap_600519_2026-11-08.json")["data"]["klines"][-1].split(",")
        d = load("ulist_named_600519_2026-11-08.json")["data"]["diff"][0]
        # 即使请求顺序被打乱，第 2 列仍是 f62、第 6 列仍是 f66
        self.assertEqual(float(row[1]), d["f62"])
        self.assertEqual(float(row[2]), d["f84"])
        self.assertEqual(float(row[3]), d["f78"])
        self.assertEqual(float(row[4]), d["f72"])
        self.assertEqual(float(row[5]), d["f66"])

    def test_kline_lmt_is_ignored(self):
        """lmt=5 仍只返回 1 条：kline 是快照接口，历史只能走 daykline。"""
        js = load("fflow_kline_lmt5_600519_2026-11-08.json")
        self.assertEqual(len(js["data"]["klines"]), 1)


class TestSecondSymbol(unittest.TestCase):
    """第二只股票（000001 平安银行）：列含义与恒等式一致，排除"只有茅台这样"。"""

    def setUp(self):
        self.rows = load("fflow_daykline_000001_2026-11-08.json")["data"]["klines"]
        self.named = load("ulist_named_000001_2026-11-08.json")["data"]["diff"][0]

    def test_multi_day_identity(self):
        self.assertEqual(len(self.rows), 5)
        for r in self.rows:
            p = [float(x) for x in r.split(",")[1:]]
            self.assertAlmostEqual(p[0], p[3] + p[4], delta=1.0, msg=r)
            self.assertLess(abs(p[0] + p[1] + p[2]), 500.0, r)

    def test_last_row_matches_named_fields(self):
        last = self.rows[-1].split(",")
        self.assertEqual(float(last[1]), self.named["f62"])
        self.assertEqual(float(last[2]), self.named["f84"])
        self.assertEqual(float(last[3]), self.named["f78"])
        self.assertEqual(float(last[4]), self.named["f72"])
        self.assertEqual(float(last[5]), self.named["f66"])


if __name__ == "__main__":
    unittest.main()
