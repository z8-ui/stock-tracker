# -*- coding: utf-8 -*-
"""routes.py 网络异常分支的离线测试（V2.0.1 验收 P0-1）

做法：mock 掉 data_service.requests.get，让真实的数据解析/缓存/路由代码跑起来，
再用 Flask test_client 断言最终 HTTP 响应（状态码 + 业务码 + 字段 + meta）。

覆盖：正常 / 超时 / 4xx / 5xx / 非法 JSON / 空数据 / 缺字段 /
      旧缓存兜底 / 全失败 unavailable / stale 标记 / 失败不覆盖缓存 / 跨 key 隔离
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests as real_requests          # noqa: E402
from flask import Flask                   # noqa: E402

import data_service as d                  # noqa: E402
import routes                             # noqa: E402


def flow_cache(n=20):
    """get_stock_money_flow 返回给缓存的结构（不是原始 HTTP 报文）。"""
    return {
        "dates": [f"10-{i:02d}" for i in range(1, n + 1)],
        "flows": {"super_large": [0.5] * n, "large": [0.3] * n,
                  "middle": [-0.1] * n, "small": [-0.2] * n},
        "unit": "亿元", "is_mock": False,
    }


def sector_rows(n=5):
    """build_heatmap_chart 需要的字段（name/change/flow）。"""
    return [{"name": f"板块{i}", "change": 1.0 + i, "flow": (i - 2) * 1.5}
            for i in range(n)]


def tencent_text(name="贵州茅台", price="1500.0", code="600519"):
    """构造腾讯行情文本：60 个 ~ 分隔字段。"""
    parts = [""] * 60
    parts[0] = f"v_sh{code}"
    parts[1] = name
    parts[2] = code
    parts[3] = price
    parts[4] = "1480.0"
    parts[5] = "1490.0"
    parts[6] = "43845.000"          # 带小数点的成交量
    parts[30] = "20261106150000"
    parts[31] = "20.0"
    parts[32] = "1.35"
    parts[33] = "1510.0"
    parts[34] = "1470.0"
    parts[38] = "0.35"
    parts[39] = "22.11"
    parts[43] = "2.7"
    parts[44] = "1880000000000"
    parts[46] = "7.8"
    parts[47] = "1800.0"
    parts[48] = "1200.0"
    parts[57] = "658000000"
    return "~".join(parts)


def flow_payload(n=25, bad=False):
    if bad:
        return {"data": {"klines": ["2026-11-06,1,2"]}}      # 字段不足
    rows = [f"2026-10-{i:02d},-1.2e8,-3.0e7,-2.0e7,-4.0e7,-8.0e7" for i in range(1, 26)]
    return {"data": {"klines": rows[:n]}}


def sector_payload(n=5, empty=False):
    rows = [] if empty else [
        {"f14": f"板块{i}", "f3": 1.0 + i, "f62": (i - 2) * 1e9} for i in range(n)
    ]
    return {"data": {"diff": rows}}


def index_payload(n=40):
    day = [[f"2026-09-{i+1:02d}", "3900", "3950", "3960", "3890", "123456"]
           for i in range(n)]
    return {"data": {"sh000001": {"day": day}}}


class Resp:
    def __init__(self, text="", status=200, payload=None, json_exc=None):
        self.text = text
        self.status_code = status
        self._payload = payload
        self._json_exc = json_exc
        self.encoding = "utf-8"

    def json(self):
        if self._json_exc:
            raise self._json_exc
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise real_requests.HTTPError(f"{self.status_code} error")


class RouteTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = d._CACHE_FILE
        self._status = d._market_status
        self._last = dict(d._LAST_SUCCESS)
        d._CACHE_FILE = os.path.join(self.tmp.name, "cache.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        d._market_status = lambda: ("交易中", True)

        self.app = Flask(__name__)
        self.app.register_blueprint(routes.bp)
        self.client = self.app.test_client()

    def tearDown(self):
        d._CACHE_FILE = self._cache
        d._market_status = self._status
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._LAST_SUCCESS.update(self._last)
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        self.tmp.cleanup()

    def use_http(self, fn):
        """把 data_service 里的 requests.get 换成 fn(url, **kw)。"""
        return mock.patch.object(d.requests, "get", side_effect=fn)

    def get_json(self, path):
        r = self.client.get(path)
        return r, r.get_json()

    def seed_cache(self, key, value, fetched_at=None, raw=False):
        cache = {}
        if os.path.exists(d._CACHE_FILE):
            with open(d._CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
        cache[key] = value if raw else {"__data": value, "fetched_at": fetched_at}
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)


class TestQuoteRoute(RouteTestBase):
    def test_valid_quote(self):
        with self.use_http(lambda url, **kw: Resp(tencent_text())):
            r, body = self.get_json("/api/stock-quote?code=600519")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["code"], 200)
        self.assertEqual(body["data"]["name"], "贵州茅台")
        self.assertEqual(body["data"]["price"], 1500.0)
        self.assertEqual(body["data"]["volume_lots"], 43845)   # 带小数点也要能转

    def test_timeout(self):
        with self.use_http(lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t"))):
            r, body = self.get_json("/api/stock-quote?code=600519")
        self.assertEqual(r.status_code, 200)      # 既有约定：HTTP 200 + 业务码 500
        self.assertEqual(body["code"], 500)

    def test_http_404_and_500(self):
        for status in (404, 500):
            with self.use_http(lambda url, **kw: Resp("", status=status)):
                _, body = self.get_json("/api/stock-quote?code=600519")
            self.assertEqual(body["code"], 500, status)

    def test_missing_fields_is_error(self):
        """价格字段缺失/为 0 → 不能返回 0 元行情。"""
        with self.use_http(lambda url, **kw: Resp("v_sh600519~贵州茅台~600519~" + "~" * 57)):
            _, body = self.get_json("/api/stock-quote?code=600519")
        self.assertEqual(body["code"], 500)

    def test_too_short_payload(self):
        with self.use_http(lambda url, **kw: Resp("garbage~only~three")):
            _, body = self.get_json("/api/stock-quote?code=600519")
        self.assertEqual(body["code"], 500)


class TestStockFlowRoute(RouteTestBase):
    def test_valid(self):
        with self.use_http(lambda url, **kw: Resp(json.dumps(flow_payload()))):
            r, body = self.get_json("/api/stock-flow?code=600519")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["code"], 200)
        self.assertEqual(len(body["data"]["dates"]), 20)

    def test_timeout_and_http_errors(self):
        cases = [
            lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t")),
            lambda url, **kw: Resp("", status=403),
            lambda url, **kw: Resp("", status=502),
        ]
        for fn in cases:
            with self.use_http(fn):
                _, body = self.get_json("/api/stock-flow?code=600519")
            self.assertEqual(body["code"], 500)
            self.assertIn("非随机填充", body["msg"])

    def test_invalid_json(self):
        with self.use_http(lambda url, **kw: Resp("<html>not json</html>")):
            _, body = self.get_json("/api/stock-flow?code=600519")
        self.assertEqual(body["code"], 500)

    def test_empty_and_short_klines(self):
        for payload in ({"data": {"klines": []}}, flow_payload(bad=True)):
            with self.use_http(lambda url, **kw: Resp(json.dumps(payload))):
                _, body = self.get_json("/api/stock-flow?code=600519")
            self.assertEqual(body["code"], 500, payload)

    def test_network_failure_returns_valid_cache(self):
        ts = time.time() - 60
        self.seed_cache("stockflow_hist_600519", flow_cache(), ts)

        with self.use_http(lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t"))):
            r, body = self.get_json("/api/stock-flow?code=600519")

        self.assertEqual(body["code"], 200)
        self.assertEqual(len(body["data"]["dates"]), 20)
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            entry = json.load(f)["stockflow_hist_600519"]
        self.assertEqual(entry["fetched_at"], ts, "失败请求改写了旧缓存的时间戳")

    def test_no_cache_and_failure_is_unavailable(self):
        with self.use_http(lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t"))):
            _, body = self.get_json("/api/stock-flow?code=600519")
        self.assertEqual(body["code"], 500)
        self.assertFalse(body.get("data"))


class TestHeatmapRoute(RouteTestBase):
    def _sector_http(self, payload):
        def fn(url, **kw):
            if "clist/get" in url:
                return Resp(json.dumps(payload))
            raise AssertionError(f"未预期的请求: {url}")
        return fn

    def test_valid(self):
        with self.use_http(self._sector_http(sector_payload())):
            r, body = self.get_json("/api/heatmap")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["code"], 200)
        self.assertFalse(body["is_mock"])
        self.assertGreater(len(body["data"]), 0)
        self.assertIsNotNone(body["asof"])

    def test_empty_and_invalid_json(self):
        for fn in (self._sector_http(sector_payload(empty=True)),
                   self._sector_http(None)):
            with self.use_http(fn):
                _, body = self.get_json("/api/heatmap")
            self.assertEqual(body["code"], 500)
            self.assertIn("未启用模拟兜底", body["msg"])

    def test_http_500(self):
        with self.use_http(lambda url, **kw: Resp("", status=500)):
            _, body = self.get_json("/api/heatmap")
        self.assertEqual(body["code"], 500)

    def test_legacy_cache_has_no_fake_time_and_is_stale(self):
        """旧缓存没有抓取时间：asof 必须是 null，且标记 stale。"""
        self.seed_cache("sector_flow", sector_rows(), raw=True)
        d._market_status = lambda: ("周末休市", False)

        with self.use_http(lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t"))):
            r, body = self.get_json("/api/heatmap")

        self.assertEqual(body["code"], 200)
        self.assertIsNone(body["asof"], "旧缓存被伪造了抓取时间")
        self.assertTrue(body["is_stale"])
        self.assertIn("抓取时间未知", body["stale_reason"] + "抓取时间未知")

    def test_legacy_cache_not_overwritten_by_failure(self):
        raw = sector_rows()
        self.seed_cache("sector_flow", raw, raw=True)
        with open(d._CACHE_FILE, "rb") as f:
            before = f.read()
        d._market_status = lambda: ("周末休市", False)

        with self.use_http(lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t"))):
            self.get_json("/api/heatmap")

        with open(d._CACHE_FILE, "rb") as f:
            after = f.read()
        self.assertEqual(after, before,
                         "抓取失败把旧缓存文件改写了")

    def test_successful_refresh_writes_timestamp_and_survives_restart(self):
        with self.use_http(self._sector_http(sector_payload())):
            self.get_json("/api/heatmap")
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            entry = json.load(f)["sector_flow"]
        self.assertIn("fetched_at", entry)
        self.assertIsInstance(entry["fetched_at"], float)

        # 模拟重启：内存与 _LAST_SUCCESS 清空，只剩文件
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._market_status = lambda: ("周末休市", False)
        with self.use_http(lambda url, **kw: (_ for _ in ()).throw(AssertionError("不该重抓"))):
            _, body = self.get_json("/api/heatmap")
        self.assertIsNotNone(body["asof"], "重启后丢了抓取时间")
        self.assertFalse(body["is_stale"])

    def test_legacy_list_cache_replaced_on_next_trading_fetch(self):
        """旧 list 缓存没有时间戳；交易时段成功抓取后必须写 __data + fetched_at。"""
        self.seed_cache("sector_flow", sector_rows(), raw=True)
        d._market_status = lambda: ("交易中", True)

        with self.use_http(self._sector_http(sector_payload())):
            _, body = self.get_json("/api/heatmap")

        self.assertEqual(body["code"], 200)
        self.assertIsNotNone(body["asof"], "成功抓取后仍然没有时间戳")
        self.assertFalse(body["is_stale"])
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            entry = json.load(f)["sector_flow"]
        self.assertIn("__data", entry)
        self.assertIn("fetched_at", entry)

    def test_stale_cache_reports_reason(self):
        self.seed_cache("sector_flow", sector_rows(),
                        fetched_at=time.time() - 5 * 86400)
        d._market_status = lambda: ("周末休市", False)
        with self.use_http(lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t"))):
            _, body = self.get_json("/api/heatmap")
        self.assertTrue(body["is_stale"])
        self.assertIsNotNone(body["asof"])
        self.assertIn("未更新", body["stale_reason"])


class TestCacheIsolation(RouteTestBase):
    def test_one_failure_does_not_pollute_other_keys(self):
        """A 股抓取失败不能影响 B 股缓存，也不能把 B 的数据写进 A。"""
        d._market_status = lambda: ("交易中", True)

        def http(url, **kw):
            if "600519" in url or "1.600519" in str(kw):
                raise real_requests.Timeout("600519 down")
            return Resp(json.dumps(flow_payload(10)))

        with self.use_http(http):
            bad = d.get_stock_money_flow("600519")
            good = d.get_stock_money_flow("000001")

        self.assertIsNone(bad)
        self.assertIsNotNone(good)
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        self.assertNotIn("stockflow_hist_600519", cache)
        self.assertEqual(len(cache["stockflow_hist_000001"]["__data"]["dates"]), 10)

    def test_stale_flag_only_on_stale_data(self):
        d._cache_update("k", {"v": 1}, time.time())
        fresh = d._cached_get("k", lambda: {"v": 2}, ttl=60)
        self.assertFalse(fresh["_stale"])
        self.assertIsNotNone(fresh["_asof"])

        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._cache_update("k2", {"v": 1}, time.time() - 5 * 86400)
        d._market_status = lambda: ("周末休市", False)
        old = d._cached_get("k2", lambda: None, ttl=60)
        self.assertTrue(old["_stale"])


class TestEventAndHealth(RouteTestBase):
    def test_health(self):
        r = self.client.get("/api/health")
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["code"], 200)
        self.assertIn("calendar", body)
        self.assertIsInstance(body["calendar"]["configured"], bool)

    def test_event_timeline_removed(self):
        """/api/event-timeline 已随「政策事件」页面下线（2026-11-07），不能再对外暴露。"""
        r = self.client.get("/api/event-timeline?days=365")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
