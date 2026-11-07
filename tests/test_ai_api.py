# -*- coding: utf-8 -*-
"""AI 分析接口的离线集成测试（V2.0.1 验收 P0-2）

原则：
  - 不访问真实 DeepSeek：get_klines 与 requests.post 全部 mock
  - 真实 key 绝不进入测试：config_local 的 key 被替换成假串，且在断言里检查不泄漏
  - 全部经过 Flask test_client，验证 HTTP 状态码 + JSON + 错误文案
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask                       # noqa: E402

import ai_analysis                            # noqa: E402
import config                                 # noqa: E402
import routes                                 # noqa: E402

FAKE_KEY = "FAKE-KEY-FOR-TESTS-ONLY-0001"   # 故意不用 sk- 前缀，避免被密钥扫描误报
MODEL_TEXT = "模拟分析：MACD 金叉，量能温和放大，注意 20 日线压力。"


def _klines(n=40):
    out = []
    base = datetime(2026, 9, 1)
    for i in range(n):
        d = base + timedelta(days=i)
        close = 100 + i * 0.5
        out.append({
            "date": d.strftime("%Y-%m-%d"),
            "open": close - 0.3, "high": close + 1.2, "low": close - 1.1,
            "close": close, "volume": 10000 + i * 10,
        })
    return out


class FakeResp:
    def __init__(self, status=200, payload=None, text="", json_exc=None):
        self.status_code = status
        self._payload = payload
        self.text = text
        self._json_exc = json_exc

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ai_analysis.requests.HTTPError(
                f"{self.status_code} Server Error for url: {ai_analysis.DEEPSEEK_URL}")

    def json(self):
        if self._json_exc:
            raise self._json_exc
        return self._payload


def _ok_resp(text=MODEL_TEXT):
    return FakeResp(200, {"choices": [{"message": {"content": text}}]})


class AIApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache_file = ai_analysis.CACHE_FILE
        self._key = ai_analysis.DEEPSEEK_API_KEY
        ai_analysis.CACHE_FILE = os.path.join(self.tmp.name, "ai_cache.json")
        ai_analysis.DEEPSEEK_API_KEY = FAKE_KEY          # 绝不使用 config_local 真 key
        self._kl = mock.patch.object(ai_analysis, "get_klines", return_value=_klines())
        self._kl.start()
        routes._ai_call_log.clear()

        self.app = Flask(__name__)
        self.app.register_blueprint(routes.bp)
        self.client = self.app.test_client()

        # 默认：配置 token，模型正常
        self._token = mock.patch.object(config, "AI_API_TOKEN", "unit-token")
        self._token.start()
        self.post = mock.patch.object(ai_analysis.requests, "post", return_value=_ok_resp())
        self.mock_post = self.post.start()

    def tearDown(self):
        self.post.stop()
        self._token.stop()
        self._kl.stop()
        ai_analysis.CACHE_FILE = self._cache_file
        ai_analysis.DEEPSEEK_API_KEY = self._key
        routes._ai_call_log.clear()
        self.tmp.cleanup()

    def _get(self, headers=None, qs="?code=600519&name=贵州茅台", **kw):
        h = {"X-AI-Token": "unit-token"}
        if headers:
            h.update(headers)
        return self.client.get("/api/ai-analysis" + qs, headers=h, **kw)

    # ---------- 1. 正常 ----------
    def test_success(self):
        r = self._get(headers={"X-AI-Key": FAKE_KEY})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["code"], 200)
        self.assertEqual(body["data"]["analysis"], MODEL_TEXT)
        self.assertFalse(body["data"]["cached"])
        self.assertIn("indicators", body["data"])
        self.assertEqual(self.mock_post.call_count, 1)

    # ---------- 2/3/4. Token ----------
    def test_missing_token_403(self):
        r = self.client.get("/api/ai-analysis?code=600519", headers={"X-AI-Key": FAKE_KEY})
        self.assertEqual(r.status_code, 403)      # V2.0.1 收尾：认证失败直接用 HTTP 403
        self.assertEqual(r.get_json()["code"], 403)
        self.assertEqual(self.mock_post.call_count, 0)

    def test_wrong_token_403(self):
        r = self.client.get("/api/ai-analysis?code=600519",
                            headers={"X-AI-Token": "bad", "X-AI-Key": FAKE_KEY})
        self.assertEqual(r.get_json()["code"], 403)
        self.assertEqual(self.mock_post.call_count, 0)

    def test_malformed_token_403(self):
        # 允许 Header 两端空白（HTTP 库常见），但大小写/前缀/控制字符都不算匹配
        for bad in ("Bearer unit-token", "UNIT-TOKEN", "unit-token\x00", "unit token"):
            r = self.client.get("/api/ai-analysis?code=600519",
                                headers={"X-AI-Token": bad, "X-AI-Key": FAKE_KEY})
            self.assertEqual(r.get_json()["code"], 403, bad)
        self.assertEqual(self.mock_post.call_count, 0)

    # ---------- 5-9. 模型/网络异常 ----------
    def test_timeout_returns_502(self):
        self.mock_post.side_effect = ai_analysis.requests.Timeout("timed out")
        r = self._get(headers={"X-AI-Key": FAKE_KEY})
        self.assertEqual(r.status_code, 502)
        body = r.get_json()
        self.assertEqual(body["code"], 502)
        self.assertIn("AI 分析调用失败", body["msg"])

    def test_http_error_returns_502(self):
        self.mock_post.return_value = FakeResp(500, text="upstream boom")
        r = self._get(headers={"X-AI-Key": FAKE_KEY})
        self.assertEqual(r.status_code, 502)
        self.assertIn("AI 分析调用失败", r.get_json()["msg"])

    def test_empty_content_returns_502(self):
        self.mock_post.return_value = _ok_resp("   ")
        r = self._get(headers={"X-AI-Key": FAKE_KEY})
        self.assertEqual(r.status_code, 502)
        self.assertIn("空内容", r.get_json()["msg"])

    def test_unparseable_content_returns_502(self):
        self.mock_post.return_value = FakeResp(200, None, json_exc=ValueError("Expecting value"))
        r = self._get(headers={"X-AI-Key": FAKE_KEY})
        self.assertEqual(r.status_code, 502)

    def test_missing_choices_key_returns_502(self):
        self.mock_post.return_value = FakeResp(200, {"choices": []})
        r = self._get(headers={"X-AI-Key": FAKE_KEY})
        self.assertEqual(r.status_code, 502)

    # ---------- 10-11. 缓存 ----------
    def test_cache_hit_does_not_call_model_again(self):
        h = {"X-AI-Key": FAKE_KEY}
        self.assertEqual(self._get(headers=h).status_code, 200)
        r2 = self._get(headers=h)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()["data"]["cached"])
        self.assertEqual(self.mock_post.call_count, 1, "缓存命中仍重复调用了模型")

    def test_expired_cache_calls_model(self):
        h = {"X-AI-Key": FAKE_KEY}
        self._get(headers=h)
        with open(ai_analysis.CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        key = next(iter(cache))
        cache[key]["created_at"] = (datetime.now() - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
        with open(ai_analysis.CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)

        r = self._get(headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["data"]["cached"])
        self.assertEqual(self.mock_post.call_count, 2, "过期缓存没有触发重新调用")

    def test_refresh_forces_model_call(self):
        h = {"X-AI-Key": FAKE_KEY}
        self._get(headers=h)
        r = self._get(headers=h, qs="?code=600519&name=贵州茅台&refresh=1")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["data"]["cached"])
        self.assertEqual(self.mock_post.call_count, 2)

    def test_failed_call_is_not_cached(self):
        self.mock_post.side_effect = ai_analysis.requests.Timeout("timed out")
        self._get(headers={"X-AI-Key": FAKE_KEY})
        self.assertFalse(os.path.exists(ai_analysis.CACHE_FILE)
                         and json.load(open(ai_analysis.CACHE_FILE, encoding="utf-8")))

    # ---------- 12. key 不泄漏 ----------
    def test_key_never_leaks_in_response_or_output(self):
        self.mock_post.side_effect = RuntimeError(f"connection failed for key {FAKE_KEY}")
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            r = self._get(headers={"X-AI-Key": FAKE_KEY})
        text = r.get_data(as_text=True) + buf_out.getvalue() + buf_err.getvalue()
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn("FAKE-KEY-FOR-TESTS-ONLY", text)
        self.assertIn("[REDACTED]", r.get_json()["msg"])

    # ---------- 访问策略 ----------
    def test_no_token_allows_local_only(self):
        with mock.patch.object(config, "AI_API_TOKEN", ""):
            r = self._get(headers={"X-AI-Key": FAKE_KEY})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["data"]["analysis"], MODEL_TEXT)

    def test_no_token_rejects_lan_client(self):
        with mock.patch.object(config, "AI_API_TOKEN", ""):
            r = self.client.get("/api/ai-analysis?code=600519",
                                headers={"X-AI-Key": FAKE_KEY},
                                environ_overrides={"REMOTE_ADDR": "192.168.1.20"})
        self.assertEqual(r.get_json()["code"], 403)
        self.assertEqual(self.mock_post.call_count, 0)

    def test_ai_config_reports_policy(self):
        r = self.client.get("/api/ai-config")
        d = r.get_json()["data"]
        self.assertTrue(d["tokenConfigured"])
        self.assertEqual(d["tokenPolicy"], "header")
        with mock.patch.object(config, "AI_API_TOKEN", ""):
            d2 = self.client.get("/api/ai-config").get_json()["data"]
        self.assertFalse(d2["tokenConfigured"])
        self.assertEqual(d2["tokenPolicy"], "local_only")

    def test_no_kline_returns_500(self):
        with mock.patch.object(ai_analysis, "get_klines", return_value=[]):
            r = self._get(headers={"X-AI-Key": FAKE_KEY})
        self.assertEqual(r.get_json()["code"], 500)
        self.assertEqual(self.mock_post.call_count, 0)


if __name__ == "__main__":
    unittest.main()
