# -*- coding: utf-8 -*-
"""前端契约检查：JS 引用的容器 id 必须真的在模板里

起因：本轮改 UI 时我自己就写出过一个不存在的函数（valAiHtml），
以及把政策块指向了一个还没建出来的容器。这类错误单测和服务端测试都抓不到，
只有真浏览器点一遍才会炸 —— 所以在测试里锁死"JS 要的 id，模板里必须有"。
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, "static", "js")

# 页面脚本 -> 它依赖的模板（events.js / events.html 已于 2026-11-07 随政策事件页下线删除）
PAGES = {
    "valuation.js": "templates/valuation.html",
}

# 由 JS 自己动态创建、不在模板里的 id（写在这里等于显式承认它们是动态的）
DYNAMIC = {
    "val-refresh-btn",     # loadStockVal 注入到 val-main-actions
}


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


class TestUiContract(unittest.TestCase):
    def test_referenced_ids_exist_in_template(self):
        problems = []
        for js_name, tpl in PAGES.items():
            js = read(os.path.join("static", "js", js_name))
            html = read(tpl)
            ids = set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_\-]+)['\"]", js))
            for i in sorted(ids - DYNAMIC):
                if f'id="{i}"' not in html:
                    problems.append(f"{js_name} 引用了 #{i}，但 {tpl} 里没有")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_policy_block_ids_present(self):
        html = read("templates/valuation.html")
        for i in ("valuation-policy", "val-policy-meta", "val-live-btn",
                  "val-main-actions", "val-main-title"):
            self.assertIn(f'id="{i}"', html, f"估值页缺容器 #{i}")

    def test_no_duplicate_valuation_card(self):
        """四段式合并后不允许再有独立的"个股估值偏离"卡片。"""
        html = read("templates/valuation.html")
        self.assertNotIn('<div class="card-header">个股估值偏离</div>', html)
        self.assertIn("一、基础估值", html)
        self.assertIn("二、估值偏离", html)
        self.assertIn("三、内在价值", html)
        self.assertIn("四、政策与事件", html)

    def test_format_js_loaded_before_users(self):
        base = read("templates/base.html")
        self.assertIn("/static/js/format.js", base)
        self.assertLess(base.index("/static/js/format.js"), base.index("/static/js/app.js"))

    def test_all_js_syntax_ok(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不在 PATH")
        for name in sorted(os.listdir(JS_DIR)):
            if not name.endswith(".js"):
                continue
            r = subprocess.run([node, "--check", os.path.join(JS_DIR, name)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
