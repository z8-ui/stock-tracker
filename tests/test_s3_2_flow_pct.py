# -*- coding: utf-8 -*-
"""S3-2：当日资金占比分母语义（删除 max(turnover, 0.01) 钳制）。

原则：
  缺失成交额不能编造分母 · 零成交额不能计算占比 · 真实零资金必须保留为 0 ·
  正常成交额必须使用真实分母。
"""
import json
import math
import unittest
from types import SimpleNamespace
from unittest import mock

import data_service as d


def old_pct(v, turnover_yi):
    """迁移前（S3-1 末态）的公式：有空值保护，但分母被 max(..., 0.01) 钳制。"""
    if turnover_yi is None or turnover_yi <= 0:
        return None
    return round(v / (max(turnover_yi, 0.01) * 1e8) * 100, 2)


PCT_FIELDS = ("main_force_pct", "super_large_pct", "large_pct", "middle_pct", "small_pct")


class FlowPctCase(unittest.TestCase):
    def setUp(self):
        self.amounts = {
            "main_force": 1e8,               # 1 亿元
            "super_large": 6e7,              # 0.6 亿元
            "large": 4e7,                    # 0.4 亿元
            "middle": -2e7,                  # -0.2 亿元
            "small": -1e7,                   # -0.1 亿元
        }

    def call(self, turnover, amounts=None):
        amounts = self.amounts if amounts is None else amounts
        quote = {"code": "600519", "price": 1439.0, "turnover": turnover}
        fake = SimpleNamespace(ok=lambda: True, data={"amounts_yuan": amounts})
        with mock.patch.object(d, "get_stock_quote", return_value=quote), \
             mock.patch.object(d._providers, "call", return_value=fake):
            return d.get_stock_real_money_flow("600519")


class TestNormalTurnover(FlowPctCase):
    """测试 1：正常成交额下，占比与迁移前逐项一致（差异必须为 0）。"""

    def test_normal_turnover_matches_old_formula(self):
        rows, diffs = [], 0
        for turnover in (33.16, 12.0, 5.0, 0.5, 0.02):
            got = self.call(turnover)
            for f in PCT_FIELDS:
                amount = self.amounts[f.replace("_pct", "")]
                want = old_pct(amount, turnover)
                same = got[f] == want
                diffs += 0 if same else 1
                rows.append(f"  turnover={turnover:>6.2f} 亿  {f:<16} 旧={want!s:<12} 新={got[f]!s:<12} {'一致' if same else '差异'}")
        print("\n[正常成交额 新旧占比逐项对照]")
        print("\n".join(rows))
        print(f"  差异项：{diffs}")
        self.assertEqual(diffs, 0, "正常成交额下不允许出现任何差异")

    def test_normal_turnover_uses_real_denominator(self):
        """手工算一遍：1 亿 / 33.16 亿 = 3.02%。"""
        got = self.call(33.16)
        self.assertEqual(got["main_force_pct"], round(1e8 / (33.16 * 1e8) * 100, 2))
        self.assertEqual(got["main_force_pct"], 3.02)


class TestMissingOrZeroTurnover(FlowPctCase):
    """测试 2：成交额缺失 / 为 0 / 无效 → 五种占比全为 None（页面显示 --）。"""

    def _assert_all_none(self, turnover, label):
        got = self.call(turnover)
        for f in PCT_FIELDS:
            self.assertIsNone(got[f], f"{label}: {f} 必须是 None，不能是 0 或任何数字")
        return got

    def test_missing_turnover(self):
        self._assert_all_none(None, "turnover=None")

    def test_zero_turnover(self):
        self._assert_all_none(0, "turnover=0")

    def test_zero_float_turnover(self):
        self._assert_all_none(0.0, "turnover=0.0")

    def test_negative_turnover(self):
        self._assert_all_none(-3.2, "turnover<0")

    def test_non_numeric_turnover(self):
        for bad in ("abc", "12,3", [], {}):
            with self.subTest(value=repr(bad)):
                self._assert_all_none(bad, f"turnover={bad!r}")

    def test_nan_and_inf_turnover(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                self._assert_all_none(bad, f"turnover={bad}")

    def test_bool_is_not_a_number(self):
        self._assert_all_none(True, "turnover=True")

    def test_payload_serializes_null_not_zero(self):
        got = self.call(None)
        payload = json.loads(json.dumps(got))
        for f in PCT_FIELDS:
            self.assertIsNone(payload[f], f"{f} 在 JSON 里必须是 null，前端才会显示 --")


class TestRealZeroAmounts(FlowPctCase):
    """测试 3：成交额有效时，真实为 0 的资金净额占比必须是 0，不能变 None。"""

    def test_all_zero_amounts_give_zero_pct(self):
        zero = {k: 0 for k in ("main_force", "super_large", "large", "middle", "small")}
        got = self.call(10.0, zero)
        for f in PCT_FIELDS:
            self.assertIsNotNone(got[f], f"{f}: 真实零不能变成 None")
            self.assertEqual(got[f], 0.0, f"{f}: 1 分钱都没流入 → 0%")
        for f in ("main_force", "super_large", "large", "middle", "small"):
            self.assertEqual(got[f], 0.0, f"{f} 金额也必须是真实 0（不是 None）")

    def test_zero_and_nonzero_mixed(self):
        amounts = {"main_force": 0, "super_large": 1_0000_0000.0,
                   "large": 0, "middle": 0, "small": 0}
        got = self.call(100.0, amounts)
        self.assertEqual(got["main_force_pct"], 0.0)
        self.assertEqual(got["super_large_pct"], 1.0)     # 1 亿 / 100 亿 = 1%
        self.assertEqual(got["large_pct"], 0.0)


class TestClampRemoved(FlowPctCase):
    """钳制删除的直接证据：极小但有效的成交额，分母不再被抬到 0.01 亿。"""

    def test_tiny_turnover_uses_exact_denominator(self):
        turnover = 0.005          # 500 万元
        got = self.call(turnover)
        expected = round(1e8 / (0.005 * 1e8) * 100, 2)     # 20000.0%
        self.assertEqual(got["main_force_pct"], expected)
        self.assertEqual(got["main_force_pct"], 20000.0)

    def test_old_formula_would_have_been_wrong(self):
        turnover = 0.005
        got = self.call(turnover)
        want_old = old_pct(1e8, turnover)
        self.assertEqual(want_old, 10000.0, "旧公式把 0.005 亿抬到 0.01 亿 → 少算一半")
        self.assertNotEqual(got["main_force_pct"], want_old,
                            "本轮必须让两者不同，否则等于没删钳制")
        self.assertGreater(got["main_force_pct"], want_old)

    def test_no_floor_for_just_above_floor(self):
        """0.0101 亿这个边界：新旧应当一致（钳制本来就没生效）。"""
        got = self.call(0.0101)
        self.assertEqual(got["main_force_pct"], old_pct(1e8, 0.0101))


class TestAmountsAndContract(FlowPctCase):
    """金额不因占比算不出而丢失；API 字段名与结构不变。"""

    def test_amounts_kept_when_pct_is_none(self):
        got = self.call(None)
        self.assertEqual(got["main_force"], 1.0)          # 1 亿元
        self.assertEqual(got["super_large"], 0.6)
        self.assertEqual(got["middle"], -0.2)

    def test_field_names_and_count_unchanged(self):
        got = self.call(33.16)
        self.assertEqual(set(got), {
            "main_force", "super_large", "large", "middle", "small",
            "main_force_pct", "super_large_pct", "large_pct", "middle_pct", "small_pct",
        })

    def test_units_are_yi_and_percent(self):
        amounts = {"main_force": 123_456_789.0, "super_large": 0, "large": 0,
                   "middle": 0, "small": 0}
        got = self.call(100.0, amounts)
        self.assertEqual(got["main_force"], round(123_456_789.0 / 1e8, 2))   # 亿元
        self.assertAlmostEqual(got["main_force_pct"], 1.23, places=2)        # %

    def test_negative_amount_gives_negative_pct(self):
        got = self.call(10.0)
        self.assertLess(got["middle_pct"], 0)
        self.assertEqual(got["middle_pct"], -2.0)

    def test_quote_missing_still_returns_none(self):
        with mock.patch.object(d, "get_stock_quote", return_value=None):
            self.assertIsNone(d.get_stock_real_money_flow("600519"))


if __name__ == "__main__":
    unittest.main()
