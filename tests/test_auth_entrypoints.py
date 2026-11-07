# -*- coding: utf-8 -*-
"""不同启动入口的认证一致性（V2.0.1 收尾 P0）

认证装在 create_app() 里，模块导入即生效；本项目验证：
  - 直接跑 app.py（真实进程）
  - flask --app app run 的导入阶段（flask CLI 解析路由，不监听端口）
  - WSGI 导入 `from app import app`
三种入口拿到的都是同一个带守卫的 app，而不是只有 __main__ 才有认证。
"""

import os
import subprocess
import sys
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env():
    e = dict(os.environ)
    e["PYTHONPATH"] = ROOT + os.pathsep + e.get("PYTHONPATH", "")
    return e


class TestEntryPoints(unittest.TestCase):
    def _run(self, args, timeout=40):
        return subprocess.run(args, cwd=ROOT, env=_env(), capture_output=True,
                              text=True, timeout=timeout)

    def test_wsgi_import_has_guard_and_serves(self):
        code = ("import app; "
                "print('guards', len(app.app.before_request_funcs.get(None) or [])); "
                "print('status', app.app.test_client().get('/api/health').status_code)")
        r = self._run([sys.executable, "-c", code])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("guards 1", r.stdout)
        self.assertIn("status 200", r.stdout)

    def test_flask_cli_imports_same_app(self):
        r = self._run([sys.executable, "-m", "flask", "--app", "app", "routes"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/api/health", r.stdout)

    def test_python_app_py_serves_loopback(self):
        proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 25
            body = None
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen("http://127.0.0.1:5000/api/health", timeout=2) as r:
                        body = r.read().decode("utf-8", "replace")
                        break
                except Exception:
                    time.sleep(0.4)
            self.assertIsNotNone(body, "app.py 没有在 25 秒内启动（端口 5000 可能被占用）")
            self.assertIn("\"ok\"", body)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
