# -*- coding: utf-8 -*-
"""前端空值语义：null 不得被显示成 0

背景：renderPieChart 里 `Math.abs(i.value)` 对 null 返回 0（JS 语义），
后端为"源没给中单/小单"而置的 null 会被画成一整块 0 值扇区并显示 0.00 亿 ——
等于把"没有数据"伪装成"真实零值"。修复后：
  · chartValue(null) === null（不画扇区），chartValue(0) === 0（真实零保留）
  · pieTip 对 null 输出"暂无数据"（防御性格式化；ECharts 不为 null 画扇区，
    正常交互下根本不会悬停到"null 扇区"，所以这不是一条浏览器可复现的路径）
  · 缺失提示按实际缺失类别动态生成，全缺/部分缺文案不同，由 gapHint 展示给用户

测试真实执行 static/js/format.js（node），并对 app.js 做接线契约检查。
"""

import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")

NODE_SCRIPT = r"""
const fs = require('fs');
global.window = {};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const f = global.window.fmt;
const four = [{name:'超大单',value:-233.21},{name:'大单',value:-291.77},
              {name:'中单',value:null},{name:'小单',value:479.15}];
const probes = {
  null_value: f.chartValue(null),
  undefined_value: f.chartValue(undefined),
  empty_value: f.chartValue(''),
  nan_value: f.chartValue(NaN),
  zero_value: f.chartValue(0),
  neg_value: f.chartValue(-3.25),
  str_value: f.chartValue('43845.000'),
  items_null_kept: f.pieItems(four),
  items_zero_kept: f.pieItems([{name:'中单',value:0},{name:'小单',value:-1}]),
  items_empty: f.pieItems([]),
  items_undefined: f.pieItems(undefined),
  tip_null: f.pieTip('中单', null, null),
  tip_undefined: f.pieTip('小单', undefined, undefined),
  tip_zero: f.pieTip('小单', 0, 25),
  tip_num: f.pieTip('大单', 3.191234, 12.5),
  tip_no_percent: f.pieTip('大单', 1.5, null),
  gap_partial: f.gapHint(f.pieItems(four)),
  gap_all: f.gapHint(f.pieItems([{name:'中单',value:null},{name:'小单',value:null}])),
  gap_none: f.gapHint(f.pieItems([{name:'中单',value:0},{name:'小单',value:1}])),
  gap_single: f.gapHint(f.pieItems([{name:'超大单',value:1},{name:'大单',value:2},
                                    {name:'中单',value:null},{name:'小单',value:3}])),
  all_null_four: f.pieItems([{name:'超大单',value:null},{name:'大单',value:null},
                             {name:'中单',value:null},{name:'小单',value:null}]),
  all_null_hint: f.gapHint(f.pieItems([{name:'超大单',value:null},{name:'大单',value:null},
                                       {name:'中单',value:null},{name:'小单',value:null}])),
  all_null_tips: [f.pieTip('超大单',null,null), f.pieTip('大单',null,null),
                  f.pieTip('中单',null,null), f.pieTip('小单',null,null)]
};
console.log(JSON.stringify(probes));
"""


@unittest.skipUnless(NODE, "环境没有 node，无法真实执行 format.js")
class TestPieNullBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        js_path = os.path.join(ROOT, "static", "js", "format.js")
        r = subprocess.run([NODE, "-e", NODE_SCRIPT, js_path],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        cls.got = json.loads(r.stdout.strip().splitlines()[-1])

    def test_null_is_not_converted_to_zero(self):
        """1. null / undefined / 空串 / NaN 都保持 null，不能变成 0。"""
        for k in ("null_value", "undefined_value", "empty_value", "nan_value"):
            with self.subTest(case=k):
                self.assertIsNone(self.got[k], "仍被转成数值（Math.abs(null)===0 的老问题）")

    def test_real_zero_preserved(self):
        """2. 真实 0 依旧是 0，不是 null。"""
        self.assertEqual(self.got["zero_value"], 0)
        self.assertIsNotNone(self.got["zero_value"])

    def test_abs_still_applied_to_numbers(self):
        self.assertEqual(self.got["neg_value"], 3.25)
        self.assertEqual(self.got["str_value"], 43845)

    def test_pie_items_keep_null_and_zero(self):
        items = self.got["items_null_kept"]
        self.assertEqual([i["name"] for i in items], ["超大单", "大单", "中单", "小单"])
        self.assertEqual(items[0]["value"], 233.21, "负数取绝对值")
        self.assertIsNone(items[2]["value"], "中单 null 必须原样保留")
        self.assertEqual(items[3]["value"], 479.15)
        self.assertEqual(self.got["items_zero_kept"][0]["value"], 0, "0 不能被当成缺失")
        self.assertEqual(self.got["items_empty"], [])
        self.assertEqual(self.got["items_undefined"], [])

    def test_tooltip_null_says_no_data(self):
        """3. pieTip 对 null 返回"暂无数据"（防御性格式化，不是悬停行为）。

        ECharts 不为 null 数据绘制扇区，用户无法悬停到不存在的扇区；
        用户实际看到的缺失说明来自 gapHint。这里只锁住"即使被调用也不会显示 0.00"。
        """
        self.assertEqual(self.got["tip_null"], "中单: 暂无数据")
        self.assertEqual(self.got["tip_undefined"], "小单: 暂无数据")
        self.assertNotIn("0.00", self.got["tip_null"])
        self.assertNotIn("0.00", self.got["tip_undefined"])

    def test_tooltip_real_zero_and_numbers(self):
        """Tooltip 对真实 0 仍显示 0.00 亿，对数值保持两位小数。"""
        self.assertEqual(self.got["tip_zero"], "小单: 0.00亿 (25.00%)")
        self.assertEqual(self.got["tip_num"], "大单: 3.19亿 (12.50%)")
        self.assertEqual(self.got["tip_no_percent"], "大单: 1.50亿")

    def test_partial_missing_hint_and_others_unaffected(self):
        """4. 部分缺失：提示点名缺失类别，其余扇区照常。"""
        # four 里只有中单是 null（小单 479.15），提示只点名中单
        self.assertEqual(self.got["gap_partial"],
                         "中单数据暂缺，当前图表仅展示可用资金类别。")
        self.assertEqual(self.got["gap_single"],
                         "中单数据暂缺，当前图表仅展示可用资金类别。")
        items = self.got["items_null_kept"]
        self.assertEqual([i["value"] for i in items if i["value"] is not None],
                         [233.21, 291.77, 479.15], "可用类别不能被缺失类别带坏")

    def test_all_missing_hint(self):
        """5. 全部缺失：明确提示无可用数据，而不是画一个 0 值饼。"""
        self.assertEqual(self.got["gap_all"], "中单、小单数据暂缺，暂无可用资金数据。")

    def test_four_categories_all_null(self):
        """四个资金类别全部为 null：文案点名四类、不产生零值、图例仍保留四类。"""
        items = self.got["all_null_four"]
        self.assertEqual([i["name"] for i in items], ["超大单", "大单", "中单", "小单"],
                         "图例类别必须保留四项")
        self.assertEqual([i["value"] for i in items], [None, None, None, None],
                         "四类都必须保留 null —— 任何一项变成 0 就是伪造零值扇区")
        self.assertEqual(self.got["all_null_hint"],
                         "超大单、大单、中单、小单数据暂缺，暂无可用资金数据。")
        for t in self.got["all_null_tips"]:
            self.assertTrue(t.endswith(": 暂无数据"), t)
            self.assertNotIn("0.00", t, "全缺时不许出现 0.00 亿")

    def test_no_gap_no_hint(self):
        self.assertEqual(self.got["gap_none"], "", "数据齐全时不该出现提示")


class TestPieWiringInAppJs(unittest.TestCase):
    """接线契约：app.js 必须走 fmt 的空值语义，不能自己 Math.abs。"""

    def setUp(self):
        with open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8") as f:
            self.src = f.read()
        start = self.src.index("function renderPieChart")
        self.body = self.src[start:start + 2000]

    def test_no_bare_math_abs_on_value(self):
        bad = re.findall(r"Math\.abs\(\s*i\.value\s*\)", self.src)
        self.assertEqual(bad, [], "renderPieChart 里仍有 Math.abs(i.value)：null 会变成 0")

    def test_uses_fmt_pie_helpers(self):
        self.assertIn("window.fmt.pieItems", self.body)
        self.assertIn("window.fmt.pieTip", self.body)

    def test_legend_keeps_all_categories(self):
        self.assertIn("data: items.map(i => i.name)", self.body,
                      "图例必须保留全部资金类别，哪怕某些类别暂无数据")

    def test_gap_hint_is_rendered_and_removed(self):
        self.assertIn("putPieGapHint", self.body)
        self.assertIn("if (!text)", self.src, "没有缺失时必须移除提示节点")
        self.assertIn("el.remove()", self.src)

    def test_legend_not_filtered_by_value(self):
        """图例取的是原始 items，不是过滤掉 null 之后的数组。"""
        self.assertIn("data: items.map(i => i.name)", self.body)
        self.assertNotIn("seriesItems.filter", self.body)
        self.assertNotIn("filter(i => i.value", self.body)

    def test_hint_text_not_hardcoded(self):
        """提示文案必须由 gapHint 动态生成，不许写死"中单、小单"。"""
        self.assertNotIn("中单、小单数据暂缺", self.src)


if __name__ == "__main__":
    unittest.main()
