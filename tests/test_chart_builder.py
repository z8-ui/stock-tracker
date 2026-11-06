# -*- coding: utf-8 -*-
"""纯函数测试：图表数据构建（不联网、不依赖 Flask）

运行：python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chart_builder import (  # noqa: E402
    build_stock_flow_chart, build_valuation_chart, build_heatmap_chart,
)


def flow_payload(**over):
    data = {
        "dates": ["10-01", "10-02"],
        "flows": {
            "super_large": [1.23, -2.5],
            "large": [0.5, 0.31],
            "middle": [-0.2, 0.1],
            "small": [-0.01, 0.0],
        },
        "is_mock": False,
        "_asof": 1793980000.0,
    }
    data.update(over)
    return data


class TestStockFlowChart(unittest.TestCase):
    def test_real_data_labelled(self):
        chart = build_stock_flow_chart("贵州茅台", flow_payload())
        self.assertIn("近2日", chart["title"])
        self.assertIn("东方财富", chart["subtitle"])
        self.assertIn("抓取", chart["subtitle"])
        self.assertFalse(chart["is_mock"])
        self.assertEqual(chart["data_date"], "10-02")
        self.assertEqual([s["name"] for s in chart["series"]], ["超大单", "大单", "中单", "小单"])
        self.assertEqual(chart["series"][0]["data"], [1.23, -2.5])

    def test_mock_is_flagged(self):
        chart = build_stock_flow_chart("X", flow_payload(is_mock=True))
        self.assertTrue(chart["is_mock"])
        self.assertIn("模拟数据", chart["subtitle"])

    def test_unknown_fetch_time(self):
        payload = flow_payload()
        payload.pop("_asof")
        chart = build_stock_flow_chart("X", payload)
        self.assertIn("抓取时间未知", chart["subtitle"])

    def test_empty_returns_none(self):
        self.assertIsNone(build_stock_flow_chart("X", None))


class TestValuationChart(unittest.TestCase):
    def _chart(self, pe, industry_pe=25, **extra):
        data = {"title": "t", "date": "2026-11-06", "pe": pe, "pb": 5.0, "pe_ttm": pe,
                "industry_pe": industry_pe}
        data.update(extra)
        return build_valuation_chart("X", data, industry_pe=industry_pe)

    def test_normal(self):
        chart = self._chart(31.0)   # (31-25)/25 = +24%
        self.assertAlmostEqual(chart["deviation_pe"], 24.0, places=1)
        self.assertEqual(chart["level"], "偏高")
        self.assertTrue(chart["available"])

    def test_fair_band(self):
        self.assertEqual(self._chart(25.0)["level"], "合理")
        self.assertEqual(self._chart(21.0)["level"], "合理")
        self.assertEqual(self._chart(19.0)["level"], "偏低")
        # 边界记录：偏离正好 +20% 仍算"合理"（原实现是严格大于 20，这里保持不变）
        self.assertEqual(self._chart(30.0)["level"], "合理")

    def test_pe_missing_not_substituted(self):
        chart = self._chart(None, level_reason="PE 缺失")
        self.assertIsNone(chart["pe"])
        self.assertIsNone(chart["deviation_pe"])
        self.assertEqual(chart["level"], "数据不可用")
        self.assertFalse(chart["available"])
        self.assertEqual(chart["level_reason"], "PE 缺失")

    def test_industry_pe_missing(self):
        chart = self._chart(30.0, industry_pe=0)
        self.assertIsNone(chart["deviation_pe"])
        self.assertEqual(chart["level"], "数据不可用")


class TestHeatmapChart(unittest.TestCase):
    def test_colors_direction(self):
        rows = build_heatmap_chart([{"name": "强流入", "flow": 5, "change": 1.0},
                                   {"name": "强流出", "flow": -5, "change": -1.0}])
        self.assertEqual(rows[0]["itemStyle"]["color"], "#ef5350")
        self.assertEqual(rows[1]["itemStyle"]["color"], "#00695c")
        self.assertEqual(rows[0]["value"], 5)   # 面积取绝对值

    def test_empty(self):
        self.assertIsNone(build_heatmap_chart(None))


if __name__ == "__main__":
    unittest.main()
