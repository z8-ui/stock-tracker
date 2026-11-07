# -*- coding: utf-8 -*-
"""安全整改的回归测试（2026-11 V2.0.1 P0）

覆盖：
  1. SERVER_HOST 默认回环
  2. 局域网绑定必须带认证，否则拒绝启动
  3. Basic 认证真的拦得住请求（不是装了个装饰器就算）
  4. AI token 只认 Header，且用 hmac.compare_digest
  5. 打包脚本：符号链接默认拒绝 / 越界 realpath 拒绝 / 大文件分块扫描 / 复检失败删包
"""

import base64
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from flask import Flask                     # noqa: E402

import app as app_mod                       # noqa: E402
import config                               # noqa: E402
import package_release as pkg               # noqa: E402
import routes                               # noqa: E402


class TestBindHost(unittest.TestCase):
    def test_default_host_is_loopback(self):
        self.assertEqual(config.SERVER_HOST, "127.0.0.1")
        self.assertTrue(app_mod.is_loopback(config.SERVER_HOST))

    def test_debug_forces_loopback(self):
        host, warning = app_mod.resolve_bind_host("0.0.0.0", True)
        self.assertEqual(host, "127.0.0.1")
        self.assertIn("DEBUG", warning)
        host, warning = app_mod.resolve_bind_host("192.168.1.8", True)
        self.assertEqual(host, "127.0.0.1")
        self.assertIsNotNone(warning)

    def test_lan_allowed_without_debug(self):
        host, warning = app_mod.resolve_bind_host("192.168.1.8", False)
        self.assertEqual(host, "192.168.1.8")
        self.assertIsNone(warning)

    def test_lan_ip_detection(self):
        self.assertFalse(app_mod.is_loopback("0.0.0.0"))
        self.assertFalse(app_mod.is_loopback("192.168.1.8"))
        self.assertTrue(app_mod.is_loopback("::1"))
        self.assertTrue(app_mod.is_loopback("localhost"))


class TestLanAuth(unittest.TestCase):
    """访问控制在 create_app() 里安装，不依赖 __main__（V2.0.1 收尾 P0）。"""

    TOKEN = "unit-test-token"

    def _client(self, token=TOKEN):
        app = Flask(__name__)

        @app.route("/")
        def index():
            return "ok"

        app_mod.install_access_guard(app, token)
        return app.test_client()

    def test_lan_without_token_refuses_to_start(self):
        self.assertIn("拒绝启动", app_mod.lan_auth_error("0.0.0.0", ""))
        self.assertIsNone(app_mod.lan_auth_error("0.0.0.0", "x"))
        self.assertIsNone(app_mod.lan_auth_error("127.0.0.1", ""))

    def test_create_app_fails_closed_when_lan_without_token(self):
        with mock.patch.object(app_mod, "SERVER_HOST", "0.0.0.0"), \
                mock.patch.object(app_mod, "SERVER_AUTH_TOKEN", ""), \
                mock.patch.object(app_mod, "DEBUG", False):
            with self.assertRaises(RuntimeError):
                app_mod.create_app()

    def test_imported_app_has_guard(self):
        """模块级 app 已经有 before_request 守卫 —— flask run / WSGI 导入也受保护。"""
        self.assertTrue(app_mod.app.before_request_funcs.get(None))

    def test_loopback_not_protected(self):
        client = self._client()
        r = client.get("/", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 200)

    def test_lan_requires_credentials(self):
        client = self._client()
        r = client.get("/", environ_overrides={"REMOTE_ADDR": "192.168.1.9"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers.get("WWW-Authenticate", ""))

    def test_lan_without_any_token_is_forbidden(self):
        """没配 token 时远程请求必须 403，而不是静默放行。"""
        client = self._client(token="")
        r = client.get("/", environ_overrides={"REMOTE_ADDR": "10.0.0.5"})
        self.assertEqual(r.status_code, 403)

    def test_wrong_and_empty_password_rejected(self):
        client = self._client()
        for raw in (b"stock:wrong", b":", b"stock:"):
            bad = base64.b64encode(raw).decode()
            r = client.get("/", headers={"Authorization": f"Basic {bad}"},
                           environ_overrides={"REMOTE_ADDR": "192.168.1.9"})
            self.assertIn(r.status_code, (401, 403), raw)

    def test_correct_password_accepted(self):
        client = self._client()
        good = base64.b64encode(f"stock:{self.TOKEN}".encode()).decode()
        r = client.get("/", headers={"Authorization": f"Basic {good}"},
                       environ_overrides={"REMOTE_ADDR": "192.168.1.9"})
        self.assertEqual(r.status_code, 200)


class TestAiToken(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(routes.bp)
        self.client = self.app.test_client()

    def test_token_from_query_string_is_rejected(self):
        """?token= 不再被接受（会留在浏览器历史/代理日志里）。"""
        with mock.patch.object(config, "AI_API_TOKEN", "s3cret"):
            with self.app.test_request_context("/?token=s3cret"):
                self.assertFalse(routes._ai_token_ok())

    def test_token_header_accepted(self):
        with mock.patch.object(config, "AI_API_TOKEN", "s3cret"):
            with self.app.test_request_context("/", headers={"X-AI-Token": "s3cret"}):
                self.assertTrue(routes._ai_token_ok())
            with self.app.test_request_context("/", headers={"X-AI-Token": "s3cres"}):
                self.assertFalse(routes._ai_token_ok())
            with self.app.test_request_context("/"):
                self.assertFalse(routes._ai_token_ok())

    def test_empty_config_means_local_only(self):
        """未配置 token ≠ 没有保护：默认只允许本机，局域网请求会被拒。"""
        with mock.patch.object(config, "AI_API_TOKEN", ""):
            with self.app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
                self.assertTrue(routes._ai_token_ok())
            with self.app.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.9"}):
                self.assertFalse(routes._ai_token_ok())


class TestPackageSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self._old_root = pkg.ROOT
        pkg.ROOT = self.root
        with open(os.path.join(self.root, "app.py"), "w") as f:
            f.write("print('hi')\n")

    def tearDown(self):
        pkg.ROOT = self._old_root
        self.tmp.cleanup()

    def test_symlink_rejected_by_default(self):
        os.symlink("/etc/passwd", os.path.join(self.root, "leak.txt"))
        with self.assertRaises(pkg.UnsafeEntry):
            list(pkg.iter_files())

    def test_symlink_to_outside_rejected_even_if_allowed(self):
        os.symlink("/etc/passwd", os.path.join(self.root, "leak.txt"))
        with self.assertRaises(pkg.UnsafeEntry):
            list(pkg.iter_files(allow_symlinks=True))

    def test_symlink_inside_root_allowed_when_explicit(self):
        target = os.path.join(self.root, "real.txt")
        with open(target, "w") as f:
            f.write("data")
        os.symlink(target, os.path.join(self.root, "link.txt"))
        names = [rel for _, rel in pkg.iter_files(allow_symlinks=True)]
        self.assertIn("link.txt", names)

    def test_symlink_directory_rejected(self):
        os.makedirs(os.path.join(self.root, "sub"))
        os.symlink(os.path.join(self.root, "sub"), os.path.join(self.root, "linkdir"))
        with self.assertRaises(pkg.UnsafeEntry):
            list(pkg.iter_files())

    def test_large_file_is_scanned(self):
        """9MB 处的 key 也必须扫出来（旧实现 >8MB 直接跳过）。"""
        path = os.path.join(self.root, "big.log.txt")
        key = b"sk-" + b"A" * 32
        with open(path, "wb") as f:
            f.write(b"x" * (9 * 1024 * 1024))
            f.write(b"\napi_key=" + key + b"\n")
        hits = pkg.scan_file(path, "big.log.txt")
        self.assertTrue(hits, "大文件里的密钥被漏掉了")

    def test_key_split_across_chunk_boundary(self):
        key = b"sk-" + b"B" * 40
        payload = b"x" * (pkg.CHUNK_SIZE - 10) + key
        holder = {"done": False}

        def reader(n):
            if holder["done"]:
                return b""
            holder["done"] = True
            return payload

        hits = pkg.scan_stream(reader, "boundary.txt", chunk_size=pkg.CHUNK_SIZE)
        self.assertTrue(hits, "跨块边界的密钥被漏掉了")

    def test_failed_zip_recheck_deletes_artifact(self):
        """复检发现私密条目 → 必须删掉刚写出的 zip，而不是留在 dist/ 等人发。"""
        out = os.path.join(self.root, "out.zip")
        with mock.patch.object(pkg, "iter_files",
                               return_value=[(os.path.join(self.root, "app.py"), "config_local.py")]):
            with mock.patch.object(sys, "argv",
                                   ["package_release.py", "--out", out]):
                rc = pkg.main()
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(out), "未通过复检的包没有被删除")


if __name__ == "__main__":
    unittest.main()
