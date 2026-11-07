# -*- coding: utf-8 -*-
"""AI 页面前端静态检查（P1：认证重试调用不存在的函数）

背景：templates/ai.html 里认证失败后的重试写成了 `return askAI(...)`，
项目里根本没有 askAI 这个函数。服务端 Token 测试全都通过，但真人输对令牌后
浏览器抛 ReferenceError，页面永久停在 "AI 分析中..."。

纯服务端测试抓不到这种错误，所以在测试里做两件事：
  1. 抽出模板里的内联 <script>，用 node --check 做语法检查（node 不在就跳过）
  2. 静态核对"被调用的标识符"是否都有定义（排除浏览器内置对象）
     —— 这一条正是当初漏掉 askAI 的原因
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_HTML = os.path.join(ROOT, "templates", "ai.html")

# 浏览器/JS 内置：出现为"函数调用"但不属于本项目定义
BUILTINS = set("""
if for while switch catch return typeof new void delete in of do else try
function
Math JSON Date Number String Boolean Array Object parseInt parseFloat isNaN
encodeURIComponent decodeURIComponent setTimeout clearTimeout setInterval
fetch prompt alert confirm console document window localStorage sessionStorage
Set Map Promise RegExp Error Blob URL FileReader FormData Intl
""".split())


def inline_scripts(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    return "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S))


def strip_comments(js):
    """去掉整行注释：注释里提到的函数名不该被当成调用（也避免注释自我触发）。"""
    return "\n".join(l for l in js.split("\n") if not l.strip().startswith("//"))


def defined_names(js):
    names = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    names |= set(re.findall(r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:function|\()", js))
    # 形如 obj.method 的调用不算本文件的定义
    return names


def called_names(js):
    # 前面不是 . 或字母数字，避免把 obj.foo( 里的 foo 算成全局调用
    return set(re.findall(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(", js))


class TestAiTemplateJs(unittest.TestCase):
    def setUp(self):
        self.js = inline_scripts(AI_HTML)
        self.code = strip_comments(self.js)

    def test_script_block_found(self):
        self.assertGreater(len(self.js), 2000, "没抽到内联脚本，检查选择器是否失效")

    def test_no_undefined_global_calls(self):
        unknown = sorted(called_names(self.code) - defined_names(self.code) - BUILTINS)
        self.assertEqual(unknown, [], f"ai.html 调用了未定义的函数：{unknown}")

    def test_askai_is_gone(self):
        self.assertNotIn("askAI(", self.code,
                         "askAI() 从未定义，输对令牌后必抛 ReferenceError")

    def test_retry_uses_real_loader_with_full_args(self):
        # 重试必须走真实函数，且不能把 refresh 传进 tag 位
        self.assertIn("loadAiAnalysis(code, name, market, tag, refresh)", self.js)

    def test_retry_rereads_token_from_session_storage(self):
        # 每次进入 loadAiAnalysis 都重新读 sessionStorage，重试才拿得到新令牌
        self.assertIn("sessionStorage.getItem('aiAccessToken')", self.js)

    def test_syntax_ok_with_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不在 PATH，跳过语法检查")
        tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
        tmp.write(self.js)
        tmp.close()
        try:
            r = subprocess.run([node, "--check", tmp.name], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
