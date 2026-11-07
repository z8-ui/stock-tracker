"""V2.0.1 P1：Flask 认证 fail-closed 专项测试。

原缺陷（app.py install_access_guard）：
    remote = (request.remote_addr or "").strip()
    if not remote or is_loopback(remote):
        return None          # ← remote_addr 为空时直接放行

要求：只有「能解析且确为回环」的来源才免认证；空值 / 非法值 / 伪造代理头
一律走认证，认证失败必须拒绝。所有用例都经过真实 Flask test_client。
"""
import base64
import os
import sys
import unittest

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class TestAuthFailClosed(unittest.TestCase):
    TOKEN = "unit-test-token"

    def _client(self, token=TOKEN):
        app = Flask(__name__)

        @app.route("/")
        def index():
            return "ok"

        app_mod.install_access_guard(app, token)
        return app.test_client()

    def _get(self, remote, token=TOKEN, headers=None):
        hdrs = dict(headers or {})
        env = {"REMOTE_ADDR": remote}
        return self._client(token).get("/", headers=hdrs, environ_overrides=env)

    # ---------- 1-2 明确回环：免认证 ----------
    def test_ipv4_loopback_allowed(self):
        self.assertEqual(self._get("127.0.0.1").status_code, 200)

    def test_ipv6_loopback_allowed(self):
        self.assertEqual(self._get("::1").status_code, 200)

    def test_ipv4_mapped_loopback_allowed(self):
        """双栈监听时 Werkzeug 可能给 ::ffff:127.0.0.1 —— 按内嵌 IPv4 判定，仍算本机。"""
        self.assertEqual(self._get("::ffff:127.0.0.1").status_code, 200)

    def test_loopback_with_whitespace_allowed(self):
        self.assertEqual(self._get("  127.0.0.1  ").status_code, 200)

    # ---------- 3-4 局域网 / 公网：必须认证 ----------
    def test_lan_ip_requires_auth(self):
        r = self._get("192.168.1.9")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers.get("WWW-Authenticate", ""))

    def test_public_ip_requires_auth(self):
        self.assertEqual(self._get("203.0.113.7").status_code, 401)

    # ---------- 5-6 空 / 非法 remote_addr：不得放行 ----------
    def test_empty_remote_addr_denied(self):
        """原缺陷点：空 remote_addr 曾直接 200 放行。"""
        r = self._get("")
        self.assertNotEqual(r.status_code, 200)
        self.assertEqual(r.status_code, 401)

    def test_none_like_remote_addr_denied(self):
        for raw in ("", "   ", "not-an-ip", "localhost", "127.0.0.1.evil.com"):
            with self.subTest(remote=raw):
                self.assertNotEqual(self._get(raw).status_code, 200)

    def test_malformed_remote_without_token_is_forbidden(self):
        """没配 token 时，空/非法来源也必须 403（不是静默放行）。"""
        for raw in ("", "not-an-ip"):
            with self.subTest(remote=raw):
                self.assertEqual(self._get(raw, token="").status_code, 403)

    # ---------- 7 代理伪造：不能让 XFF 骗过认证 ----------
    def test_x_forwarded_for_cannot_fake_loopback(self):
        r = self._get("192.168.1.9", headers={"X-Forwarded-For": "127.0.0.1",
                                              "X-Real-IP": "127.0.0.1",
                                              "Forwarded": "for=127.0.0.1"})
        self.assertEqual(r.status_code, 401)

    def test_forwarded_loopback_still_needs_auth(self):
        """即使来源是本机，只要带了代理头也不改变判定；正相反，来源必须是真回环。"""
        r = self._get("203.0.113.7", headers={"X-Forwarded-For": "10.0.0.1"})
        self.assertEqual(r.status_code, 401)

    # ---------- 8-10 token 校验 ----------
    def test_missing_token_rejected(self):
        self.assertEqual(self._get("192.168.1.9").status_code, 401)

    def test_wrong_token_rejected(self):
        r = self._get("192.168.1.9", headers={"Authorization": _basic("stock", "nope")})
        self.assertEqual(r.status_code, 401)

    def test_wrong_username_rejected(self):
        r = self._get("192.168.1.9", headers={"Authorization": _basic("admin", self.TOKEN)})
        self.assertEqual(r.status_code, 401)

    def test_malformed_authorization_header_rejected(self):
        for raw in ("Basic", "Basic !!!notbase64", "Bearer abc"):
            with self.subTest(header=raw):
                self.assertEqual(self._get("192.168.1.9",
                                           headers={"Authorization": raw}).status_code, 401)

    def test_correct_token_accepted(self):
        r = self._get("192.168.1.9", headers={"Authorization": _basic("stock", self.TOKEN)})
        self.assertEqual(r.status_code, 200)

    def test_correct_token_accepted_over_public_ip(self):
        r = self._get("203.0.113.7", headers={"Authorization": _basic("stock", self.TOKEN)})
        self.assertEqual(r.status_code, 200)

    # ---------- 辅助函数本身的契约 ----------
    def test_trusted_loopback_predicate(self):
        self.assertTrue(app_mod._trusted_loopback("127.0.0.1"))
        self.assertTrue(app_mod._trusted_loopback("::1"))
        self.assertTrue(app_mod._trusted_loopback("::ffff:127.0.0.1"))
        self.assertTrue(app_mod._trusted_loopback("127.0.0.1 "))   # 两侧空白容忍
        for bad in ("", None, "  ", "localhost", "10.0.0.1", "0.0.0.0",
                    "127.0.0.1.evil.com", "LOCALHOST"):
            with self.subTest(value=bad):
                self.assertFalse(app_mod._trusted_loopback(bad))

    def test_guard_reads_only_remote_addr(self):
        """守卫不许读 X-Forwarded-For：源码里出现该头就说明有绕过风险。"""
        import inspect
        src = inspect.getsource(app_mod.install_access_guard)
        for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
            self.assertNotIn(header, src)


if __name__ == "__main__":
    unittest.main()
