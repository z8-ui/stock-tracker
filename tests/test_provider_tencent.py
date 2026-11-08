# -*- coding: utf-8 -*-
"""V2.1 S1：腾讯行情 Provider + 兼容性/降级测试

三组：
  A 基础：正常 / 空 / 超时 / 网络异常 / 格式错误 / 来源标识 / 时间戳 / 错误状态
  B 降级：主源成功、主源失败备用源接管、全失败、未注册源、缓存降级、过期缓存
  C 兼容：_tencent_quote 与旧实现逐字节等价、get_stock_quote 字段不变、
          get_market_flow 链路不变

所有外部请求都 mock，不消耗真实数据源额度。
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

import requests as real_requests                    # noqa: E402

import data_service as d                            # noqa: E402
from services.data import registry                  # noqa: E402
from services.data.base import Provider, ProviderResult   # noqa: E402
from services.data.providers import tencent as tp   # noqa: E402


# ---------------------------------------------------------------- 报文构造

def quote_text(code="600519", name="贵州茅台", price="1453.00",
               trade_time="20261106150000", n=88, volume="43845.000"):
    """构造一条腾讯快照报文（字段按真实下标摆放）。"""
    parts = [""] * 88
    parts[0] = "1"
    parts[1] = name
    parts[2] = code
    parts[3] = price
    parts[4] = "1440.00"
    parts[5] = "1445.00"
    parts[6] = volume
    parts[30] = trade_time
    parts[31] = "13.00"
    parts[32] = "0.90"
    parts[33] = "1460.00"
    parts[34] = "1438.00"
    parts[38] = "0.35"
    parts[39] = "22.11"
    parts[43] = "1.52"
    parts[44] = "1825000000000"
    parts[46] = "7.80"
    parts[47] = "1800.00"
    parts[48] = "1200.00"
    parts[57] = "658000000"
    return "~".join(parts[:n])                  # 需要截断的用例（如 n=50）直接切


def old_tencent_quote(code):
    """改动前的实现（原样抄一份，作为差分对照的参照物）。"""
    market = "sh" if code.startswith("6") or code == "000001" else "sz"
    url = f"http://qt.gtimg.cn/q={market}{code}"
    try:
        resp = real_requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        resp.encoding = "gbk"
        parts = resp.text.split("~")
        return parts if len(parts) > 50 else None
    except Exception:
        return None


class Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status
        self.encoding = None

    def json(self):
        return json.loads(self.text)


# ---------------------------------------------------------------- A 基础

class TestTencentProvider(unittest.TestCase):
    def setUp(self):
        self.p = tp.TencentQuoteProvider()

    def fetch_with(self, side_effect):
        with mock.patch.object(tp.requests, "get", side_effect=side_effect):
            return self.p.fetch("600519")

    def test_success(self):
        r = self.fetch_with(lambda url, **kw: Resp(quote_text()))
        self.assertEqual(r.status, "success")
        self.assertTrue(r.ok())
        self.assertEqual(r.source, "tencent_quote", "必须带上数据源标识")
        self.assertEqual(len(r.data), 88)
        self.assertEqual(r.data[1], "贵州茅台")

    def test_asof_comes_from_source_not_now(self):
        r = self.fetch_with(lambda url, **kw: Resp(quote_text(trade_time="20261106145533")))
        self.assertEqual(r.asof, "2026-11-06T14:55:33")
        self.assertGreater(r.fetched_at, 0, "fetched_at 是本地抓取时刻")
        self.assertLess(r.elapsed_ms, 5000)

    def test_asof_empty_when_source_gives_nothing(self):
        r = self.fetch_with(lambda url, **kw: Resp(quote_text(trade_time="")))
        self.assertEqual(r.asof, "", "源不给时间就留空，绝不拿现在顶替")

    def test_empty_body(self):
        r = self.fetch_with(lambda url, **kw: Resp(""))
        self.assertEqual(r.status, "empty")
        self.assertFalse(r.ok())
        self.assertEqual(r.error["type"], "ProviderEmpty")
        self.assertIsNone(r.data)

    def test_timeout(self):
        def boom(url, **kw):
            raise real_requests.Timeout("read timeout")
        r = self.fetch_with(boom)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.error["type"], "ProviderTimeout")
        self.assertIn("超时", r.error["message"])

    def test_network_error(self):
        def boom(url, **kw):
            raise real_requests.ConnectionError("conn reset")
        r = self.fetch_with(boom)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.error["type"], "ProviderNetworkError")

    def test_format_error_on_truncated_payload(self):
        r = self.fetch_with(lambda url, **kw: Resp("~".join(["x"] * 10)))
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.error["type"], "ProviderFormatError")

    def test_unexpected_exception_is_contained(self):
        r = self.fetch_with(lambda url, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(r.status, "failed")
        self.assertIn("未预期异常", r.error["message"])

    def test_market_prefix_matches_old_behaviour(self):
        urls = []
        with mock.patch.object(tp.requests, "get",
                               side_effect=lambda url, **kw: (urls.append(url), Resp(quote_text()))[1]):
            for code in ("600519", "000001", "300750", "002594", "900901"):
                self.p.fetch(code)
        self.assertEqual(urls, [
            "http://qt.gtimg.cn/q=sh600519",
            "http://qt.gtimg.cn/q=sh000001",     # 000001 视为上证指数（历史行为）
            "http://qt.gtimg.cn/q=sz300750",
            "http://qt.gtimg.cn/q=sz002594",
            "http://qt.gtimg.cn/q=sh900901",
        ])

    def test_gbk_decoding(self):
        class GbkResp(Resp):
            pass
        r = self.fetch_with(lambda url, **kw: GbkResp(quote_text(name="贵州茅台")))
        self.assertEqual(r.data[1], "贵州茅台")

    def test_describe(self):
        self.assertEqual(self.p.describe(), {"name": "tencent_quote",
                                             "kind": "quote", "timeout": 5.0})


# ---------------------------------------------------------------- B 降级

class StubProvider(Provider):
    name = "stub"
    kind = "test"

    def __init__(self, name, status="success", data=None, asof=""):
        self.name = name
        self.status = status
        self._data = data if data is not None else {"ok": True}
        self._asof = asof

    def request(self, *a, **kw):
        return "raw"

    def normalize(self, raw, *a, **kw):
        from services.data.exceptions import ProviderError
        if self.status == "failed":
            raise ProviderError(self.name, "stub 失败")
        if self.status == "empty":
            return None, ""
        return self._data, self._asof


class TestRegistryAndFallback(unittest.TestCase):
    def setUp(self):
        self._saved = dict(registry._REGISTRY)

    def tearDown(self):
        registry._REGISTRY.clear()
        registry._REGISTRY.update(self._saved)

    def test_builtin_registered(self):
        self.assertIn("tencent_quote", registry.names())
        self.assertEqual(registry.get("tencent_quote").kind, "quote")
        self.assertTrue(registry.describe())

    def test_unregistered_name(self):
        from services.data.exceptions import ProviderUnavailable
        with self.assertRaises(ProviderUnavailable) as ctx:
            registry.get("nope")
        self.assertIn("tencent_quote", str(ctx.exception), "报错要列出可用名单")

    def test_duplicate_registration_rejected(self):
        with self.assertRaises(ValueError):
            registry.register(tp.TencentQuoteProvider())

    def test_chain_primary_success(self):
        registry.register(StubProvider("p1"), replace=True)
        registry.register(StubProvider("p2"), replace=True)
        r = registry.call_chain(["p1", "p2"])
        self.assertTrue(r.ok())
        self.assertEqual(r.source, "p1")
        self.assertEqual(len(r.attempts), 1)

    def test_chain_primary_fails_secondary_takes_over(self):
        registry.register(StubProvider("bad", status="failed"), replace=True)
        registry.register(StubProvider("good", data={"v": 1}), replace=True)
        r = registry.call_chain(["bad", "good"])
        self.assertTrue(r.ok())
        self.assertEqual(r.source, "good", "备用源接管后来源必须是备用源")
        self.assertEqual(r.data, {"v": 1})
        self.assertEqual([a["source"] for a in r.attempts], ["bad", "good"])
        self.assertEqual(r.attempts[0]["status"], "failed")

    def test_chain_primary_empty_secondary_takes_over(self):
        registry.register(StubProvider("empty1", status="empty"), replace=True)
        registry.register(StubProvider("good", data=[1]), replace=True)
        r = registry.call_chain(["empty1", "good"])
        self.assertTrue(r.ok())
        self.assertEqual(r.attempts[0]["status"], "empty")

    def test_chain_all_fail(self):
        registry.register(StubProvider("bad1", status="failed"), replace=True)
        registry.register(StubProvider("bad2", status="failed"), replace=True)
        r = registry.call_chain(["bad1", "bad2"])
        self.assertFalse(r.ok())
        self.assertEqual(r.status, "failed")
        self.assertEqual([a["source"] for a in r.attempts], ["bad1", "bad2"])
        self.assertIsNotNone(r.error)

    def test_chain_skips_unregistered(self):
        registry.register(StubProvider("good", data={"v": 2}), replace=True)
        r = registry.call_chain(["ghost", "good"])
        self.assertTrue(r.ok())
        self.assertEqual(r.attempts[0]["status"], "unavailable")


class TestCacheDegradation(unittest.TestCase):
    """缓存层降级：Provider 挂了，页面还能拿到旧快照（并且明确标 stale）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = d._CACHE_FILE
        self._status = d._market_status
        d._CACHE_FILE = os.path.join(self.tmp.name, "cache.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        d._market_status = lambda: ("交易中", True)      # 走"盘中"路径，才能试到实时抓取失败

    def tearDown(self):
        d._CACHE_FILE = self._cache
        d._market_status = self._status
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        self.tmp.cleanup()

    def seed(self, age_sec=1800):
        payload = {"main_force": -524.97, "super_large": -300.0, "large_order": -224.97,
                   "middle_order": 100.0, "small_order": 424.97,
                   "date": "20261106", "index_name": "上证指数",
                   "index_price": 3649.18, "index_change": -1.1}
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"market_flow": {"__data": payload,
                                       "fetched_at": time.time() - age_sec}}, f)

    def test_provider_failure_falls_back_to_file_cache(self):
        self.seed(age_sec=1800)      # 超过盘中 STALE_AFTER_SEC(900)，才该标 stale

        def boom(*a, **kw):
            raise real_requests.Timeout("timeout")
        with mock.patch.object(d.requests, "get", side_effect=boom):
            out = d.get_market_flow()
        self.assertIsNotNone(out, "有旧缓存时不能返回 None")
        self.assertEqual(out["index_price"], 3649.18)
        self.assertTrue(out["_stale"], "降级数据必须标 stale")
        self.assertIn("未更新", out["_stale_reason"])

    def test_expired_cache_is_marked_not_hidden(self):
        self.seed(age_sec=10 * 86400)                    # 十天前的快照
        def boom(*a, **kw):
            raise real_requests.Timeout("timeout")
        with mock.patch.object(d.requests, "get", side_effect=boom):
            out = d.get_market_flow()
        self.assertTrue(out["_stale"])
        self.assertGreater(out["_age_sec"], 9 * 86400)

    def test_no_cache_and_provider_down_returns_none(self):
        """全源失败且无缓存：必须 None（页面显示暂无数据），不能返回全零假数据。"""
        def boom(*a, **kw):
            raise real_requests.Timeout("timeout")
        with mock.patch.object(d.requests, "get", side_effect=boom):
            self.assertIsNone(d.get_market_flow())


# ---------------------------------------------------------------- C 兼容

class TestCompat(unittest.TestCase):
    """迁移前后行为必须逐字节一致。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = d._CACHE_FILE
        self._status = d._market_status
        d._CACHE_FILE = os.path.join(self.tmp.name, "cache.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        d._market_status = lambda: ("交易中", True)

    def tearDown(self):
        d._CACHE_FILE = self._cache
        d._market_status = self._status
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        self.tmp.cleanup()

    def test_tencent_quote_identical_to_old_implementation(self):
        cases = {
            "正常 88 字段": quote_text(),
            "刚好 51 字段": quote_text(n=51),
            "50 字段（旧实现返回 None）": quote_text(n=50),
            "空串": "",
            "乱码片段": "no-tilde-at-all",
            "仅一个 ~": "~",
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                with mock.patch.object(tp.requests, "get", side_effect=lambda url, **kw: Resp(text)):
                    old = old_tencent_quote("600519")
                with mock.patch.object(tp.requests, "get", side_effect=lambda url, **kw: Resp(text)):
                    new = d._tencent_quote("600519")
                self.assertEqual(old, new, f"{label}: 新旧返回值必须一致")

    def test_tencent_quote_failure_paths_identical(self):
        for exc in (real_requests.Timeout("t"), real_requests.ConnectionError("c"), RuntimeError("r")):
            with self.subTest(exc=type(exc).__name__):
                def boom(url, **kw):
                    raise exc
                with mock.patch.object(tp.requests, "get", side_effect=boom):
                    self.assertIsNone(old_tencent_quote("600519"))
                with mock.patch.object(tp.requests, "get", side_effect=boom):
                    self.assertIsNone(d._tencent_quote("600519"))

    def test_stock_quote_fields_unchanged(self):
        with mock.patch.object(tp.requests, "get", side_effect=lambda url, **kw: Resp(quote_text())):
            q = d.get_stock_quote("600519")
        self.assertEqual(q["name"], "贵州茅台")
        self.assertEqual(q["code"], "600519")
        self.assertEqual(q["price"], 1453.00)
        self.assertEqual(q["pre_close"], 1440.00)
        self.assertEqual(q["open"], 1445.00)
        self.assertEqual(q["high"], 1460.00)
        self.assertEqual(q["low"], 1438.00)
        self.assertEqual(q["change"], 13.00)
        self.assertEqual(q["change_pct"], 0.90)
        self.assertEqual(q["volume_lots"], 43845, "腾讯成交量是带小数的字符串，必须 float→int")
        self.assertEqual(q["pe"], 22.11)
        self.assertEqual(q["pb"], 7.80)
        self.assertEqual(q["turnover"], 65800.00)
        self.assertEqual(q["turnover_rate"], 0.35)
        self.assertEqual(q["amplitude"], 1.52)
        self.assertEqual(q["high_52w"], 1800.00)
        self.assertEqual(q["low_52w"], 1200.00)
        self.assertEqual(len(q), 18)

    def test_stock_quote_zero_price_still_none(self):
        with mock.patch.object(tp.requests, "get",
                               side_effect=lambda url, **kw: Resp(quote_text(price="0"))):
            self.assertIsNone(d.get_stock_quote("600519"))

    def test_market_flow_chain_unchanged(self):
        """指数 + 资金流：Provider 迁移后字段、来源判定、缓存落盘都不变。"""
        def fake_get(url, **kw):
            if "qt.gtimg" in url:
                return Resp(quote_text(code="000001", name="上证指数",
                                       price="3649.18", trade_time="20261106150000"))
            return Resp(json.dumps({"data": {"diff": [
                {"f62": 3.2e8, "f66": 1.1e8, "f69": -2.0e7, "f72": 8.0e7, "f75": -5.0e7}]}}))
        with mock.patch.object(d.requests, "get", side_effect=fake_get):
            out = d.get_market_flow()
        self.assertEqual(out["index_name"], "上证指数")
        self.assertEqual(out["index_price"], 3649.18)
        self.assertEqual(out["index_change"], 0.90)
        self.assertEqual(out["date"], "20261106", "交易日必须来自源快照")
        self.assertFalse(out["_stale"])
        self.assertNotIn("_undefined", out)
        self.assertIn("market_flow", d._load_cache(), "结果要落盘，重启后能当快照")


if __name__ == "__main__":
    unittest.main()
