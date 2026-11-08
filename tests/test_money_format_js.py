# -*- coding: utf-8 -*-
"""V2.0.1 P1：资金流向页数字统一两位小数（真实执行 format.js，不是文本匹配）

在 node 里加载 static/js/format.js（它把 API 挂到 window.fmt），逐条跑验收表；
再用源码契约检查「资金图表里不许再出现裸拼接 xxx + '亿'」。
node 不存在时跳过执行部分（环境限制不能算 PASS）。
"""

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
const src = fs.readFileSync(process.argv[1], 'utf8');
eval(src);
const f = global.window.fmt;
const probes = {
  'fixed_125.678912': f.fixed(125.678912),
  'fixed_-32.456789': f.fixed(-32.456789),
  'fixed_0': f.fixed(0),
  'fixed_1': f.fixed(1),
  'fixed_null': f.fixed(null),
  'fixed_undefined': f.fixed(undefined),
  'fixed_NaN': f.fixed(NaN),
  'fixed_Infinity': f.fixed(Infinity),
  'fixed_str': f.fixed('43845.000'),
  'fixed_empty': f.fixed(''),
  'money_pos': f.money(3.191234, '亿'),
  'money_neg': f.money(-3.199999, '亿'),
  'money_zero': f.money(0, '亿'),
  'money_null': f.money(null, '亿'),
  'pct_pos': f.pct(1.2345),
  'pct_neg': f.pct(-1.2355),
  'pct_zero': f.pct(0),
  'pct_null': f.pct(undefined),
  'plain_str': f.plain('43845.000'),
  'date8_8digit': f.date8('20261106'),
  'date8_number': f.date8(20261106),
  'date8_dashed': f.date8('2026-11-06'),
  'date8_spaces': f.date8(' 20261106 '),
  'date8_slash': f.date8('2026/11/06'),
  'date8_empty': f.date8(''),
  'date8_null': f.date8(null),
  'date8_undefined': f.date8(undefined),
  'date8_short': f.date8('2026'),
  'date8_bad_month': f.date8('20261399'),
  'date8_garbage': f.date8('abc'),
  'date8_truthy_check': f.date8(undefined) ? 'shown' : 'hidden'
};
console.log(JSON.stringify(probes));
"""


@unittest.skipUnless(NODE, "环境没有 node，无法真实执行 format.js")
class TestFormatJsBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json
        js_path = os.path.join(ROOT, "static", "js", "format.js")
        r = subprocess.run([NODE, "-e", NODE_SCRIPT, js_path],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        cls.got = json.loads(r.stdout.strip().splitlines()[-1])

    def test_two_decimals_table(self):
        """审查给的对照表：两位小数，无效值一律 --。"""
        table = {
            "fixed_125.678912": "125.68",
            "fixed_-32.456789": "-32.46",
            "fixed_0": "0.00",
            "fixed_1": "1.00",
            "fixed_null": "--",
            "fixed_undefined": "--",
            "fixed_NaN": "--",
            "fixed_Infinity": "--",
            "fixed_str": "43845.00",
            "fixed_empty": "--",
        }
        for k, want in table.items():
            with self.subTest(case=k):
                self.assertEqual(self.got[k], want)

    def test_money_sign_and_unit(self):
        """金额：正数带 +、负数自带 -、0 不带符号，单位原样保留，两位小数。"""
        self.assertEqual(self.got["money_pos"], "+3.19亿")
        self.assertEqual(self.got["money_neg"], "-3.20亿")
        self.assertEqual(self.got["money_zero"], "0.00亿")
        self.assertEqual(self.got["money_null"], "--")

    def test_pct_two_decimals(self):
        self.assertEqual(self.got["pct_pos"], "+1.23%")
        self.assertEqual(self.got["pct_neg"], "-1.24%")
        self.assertEqual(self.got["pct_zero"], "0.00%")
        self.assertEqual(self.got["pct_null"], "--")

    def test_date8_formats_real_trade_date(self):
        """交易日 8 位 → 2026-11-06；取不到就是空串（页面据此不显示）。"""
        self.assertEqual(self.got["date8_8digit"], "2026-11-06")
        self.assertEqual(self.got["date8_number"], "2026-11-06")
        self.assertEqual(self.got["date8_dashed"], "2026-11-06")
        self.assertEqual(self.got["date8_spaces"], "2026-11-06")
        self.assertEqual(self.got["date8_slash"], "2026-11-06")

    def test_date8_never_returns_undefined_or_junk(self):
        """空值/坏值一律空串：不许出现 undefined / null / NaN / 20261399。"""
        for k in ("date8_empty", "date8_null", "date8_undefined",
                  "date8_short", "date8_bad_month", "date8_garbage"):
            with self.subTest(case=k):
                self.assertEqual(self.got[k], "")
        self.assertEqual(self.got["date8_truthy_check"], "hidden",
                         "空日期必须是 falsy，页面才不会拼出多余空白")


class TestNoRawMoneyConcat(unittest.TestCase):
    """资金图表里不许再出现裸拼接：必须是两位小数的格式化函数。"""

    # (文件, 旧写法正则, 说明)
    FORBIDDEN = [
        ("static/js/app.js", r"formatter:\s*'\{b\}: \{c\}亿", "饼图 tooltip 直接输出原始精度"),
        ("static/js/app.js", r"p\.value\s*\+\s*'亿'", "柱状图标签裸拼 p.value"),
        ("static/js/app.js", r"p\.data\.flow\s*\+\s*'亿'", "热力图标签裸拼 flow"),
        ("static/js/app.js", r"formatter:\s*'\{value\} 亿'", "坐标轴裸输出原值"),
        ("templates/market_flow.html", r"\$\{d\.index_change\}\s*%", "指数涨跌幅裸输出"),
        ("templates/stock_flow.html", r"q\.turnover\s*\+\s*'亿'", "成交额裸拼接"),
    ]

    def test_forbidden_patterns_gone(self):
        problems = []
        for rel, pat, why in self.FORBIDDEN:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                src = f.read()
            if re.search(pat, src):
                problems.append(f"{rel}: {why}（pattern {pat}）")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_money_charts_use_fmt(self):
        with open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8") as f:
            src = f.read()
        for fn in ("renderPieChart", "renderMainForceChart", "renderTreemap", "renderStockFlow"):
            start = src.index("function " + fn)
            body = src[start:start + 3000]
            with self.subTest(fn=fn):
                self.assertIn("window.fmt", body, f"{fn} 没有用统一格式化函数")


if __name__ == "__main__":
    unittest.main()
