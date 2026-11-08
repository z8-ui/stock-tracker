# -*- coding: utf-8 -*-
"""V2.1 S1 收尾：休市 vs 数据源故障、真实零值、Provider 遥测

七种场景（全部 mock，且不依赖"今天是不是周末"——交易状态与最近交易日都显式注入）：

  1 周末休市 + 有周五缓存      → 展示最近交易日数据，状态 CACHED，不是 STALE，不记为故障
  2 周末休市 + 无缓存          → 暂无数据，明确休市，不用零值填充
  3 交易时段 + 数据源超时      → 有缓存则降级并标 stale；无缓存则 None（不伪装成零值）
  4 交易时段 + 空数据          → 同上，且 Provider 记 empty 而不是 failed
  5 交易时段 + 真实零值        → 保留零值（不能因为等于 0 就判失败）
  6 主源失败 + 备用源成功      → call_chain 接管，来源标成命中的那个源
  7 缓存日期早于最近交易日     → 真过期：STALE，并说明数据停在哪天
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

import requests as real_requests              # noqa: E402

import data_health                            # noqa: E402
import data_service as d                      # noqa: E402
from services.data import registry, telemetry  # noqa: E402
from services.data.base import Provider       # noqa: E402
from services.data.providers import tencent as tp   # noqa: E402


def index_text(price="3649.18", trade_time="20261106150000", name="上证指数"):
    parts = [""] * 88
    parts[0] = "1"
    parts[1] = name
    parts[2] = "000001"
    parts[3] = price
    parts[30] = trade_time
    parts[32] = "-1.10"
    return "~".join(parts)


def flow_payload(rows=None):
    if rows is None:
        rows = [{"f62": 3.2e8, "f66": 1.1e8, "f69": -2.0e7, "f72": 8.0e7, "f75": -5.0e7}]
    return json.dumps({"data": {"diff": rows}})


ZERO_FLOW = [{"f62": 0, "f66": 0, "f69": 0, "f72": 0, "f75": 0}]


class Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status
        self.encoding = None

    def json(self):
        return json.loads(self.text)


class Base(unittest.TestCase):
    """统一准备：临时缓存文件 + 清空内存/统计，交易状态由用例显式给定。"""

    SESSION = ("周末休市", False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = d._CACHE_FILE
        self._status = d._market_status
        d._CACHE_FILE = os.path.join(self.tmp.name, "cache.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        d._market_status = lambda: self.SESSION
        telemetry.reset()
        with d._CACHE_STATS_LOCK:
            for k in d.CACHE_STATS:
                d.CACHE_STATS[k] = 0
        # 缓存键维度的统计也要清干净，否则别的用例留下的失败会污染"休市无请求"的判断
        with d._SOURCE_LOCK:
            self._saved_sources = dict(d.SOURCE_STATS)
            d.SOURCE_STATS.clear()
        # 固定"最近交易日"= 2026-11-06（周五），不依赖真实日期
        self._patch_lt = mock.patch.object(data_health, "_last_trading_day",
                                           return_value="20261106")
        self._patch_lt.start()

    def tearDown(self):
        with d._SOURCE_LOCK:
            d.SOURCE_STATS.clear()
            d.SOURCE_STATS.update(getattr(self, "_saved_sources", {}))
        self._patch_lt.stop()
        d._CACHE_FILE = self._cache
        d._market_status = self._status
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        self.tmp.cleanup()

    def seed(self, key, data, age_sec=3600):
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({key: {"__data": data, "fetched_at": time.time() - age_sec}}, f)

    def market_data(self, date="20261106", index_price=3649.18, main_force=-524.97,
                    flow_available=True):
        return {"_flow_fields_v": 2, "main_force": main_force, "super_large": -300.0,
                "large_order": -224.97,
                "middle_order": 100.0, "small_order": 424.97, "date": date,
                "index_name": "上证指数", "index_price": index_price,
                "index_change": -1.1, "_flow_available": flow_available}

    def market_row(self):
        snap = data_health.snapshot()
        for r in snap["rows"]:
            if r["module"] == "市场资金":
                return r, snap
        self.fail("快照里没有市场资金行")


# ------------------------------------------------------------------ 1 / 2 / 7

class TestClosedSession(Base):
    SESSION = ("周末休市", False)

    def test_1_closed_with_friday_cache(self):
        """周末 + 周五缓存：CACHED，标明休市与数据日期，不判故障。"""
        self.seed("market_flow", self.market_data(), age_sec=2 * 86400)
        row, snap = self.market_row()
        self.assertEqual(row["status"], "CACHED", "休市的最近交易日数据不该被标成 STALE")
        self.assertTrue(row["ok"], "正常休市不能计入异常")
        self.assertIn("周末休市", row["detail"])
        self.assertIn("2026-11-06", row["detail"])
        self.assertIn("不是实时行情", row["detail"])
        self.assertEqual(snap["market"]["status"], "周末休市")
        self.assertTrue(snap["market"]["closed"])
        self.assertEqual(snap["market"]["last_trading_day"], "2026-11-06")

    def test_2_closed_without_cache(self):
        """周末且没有缓存：暂无数据 + 明确休市，不用零值填充。"""
        row, snap = self.market_row()
        def boom(*a, **kw):
            raise real_requests.Timeout("t")
        self.assertEqual(row["status"], "UNAVAILABLE")
        self.assertFalse(row["ok"])
        self.assertIn("周末休市", row["detail"])
        self.assertIn("无可用缓存", row["detail"])
        with mock.patch.object(d.requests, "get", side_effect=boom):
            self.assertIsNone(d.get_market_flow(), "没有缓存又抓不到，必须返回 None")

    def test_7_cache_older_than_last_trading_day_is_really_stale(self):
        """数据停在 2026-11-04（周三），最近交易日是 11-06 → 真过期。"""
        self.seed("market_flow", self.market_data(date="20261104"), age_sec=4 * 86400)
        row, _snap = self.market_row()
        self.assertEqual(row["status"], "STALE")
        self.assertIn("最近交易日是 2026-11-06", row["detail"])


class TestClosedOtherRows(Base):
    SESSION = ("周末休市", False)

    def test_stockflow_row_not_counted_as_failure_on_weekend(self):
        self.seed("stockflow_hist_600519",
                  {"dates": ["11-05", "11-06"], "flows": {"super_large": [1.0, 2.0],
                                                          "large": [0.5, 0.6],
                                                          "middle": [0, 0],
                                                          "small": [0, 0]},
                   "unit": "亿元", "is_mock": False}, age_sec=2 * 86400)
        snap = data_health.snapshot()
        row = [r for r in snap["rows"] if r["module"] == "个股历史资金"][0]
        self.assertEqual(row["status"], "CACHED")
        self.assertTrue(row["ok"])
        self.assertIn("正常不更新", row["detail"])

    def test_no_requests_on_weekend_is_not_failure(self):
        """休市期间没发生请求：既不算成功也不算失败。"""
        snap = data_health.snapshot()
        row = [r for r in snap["rows"] if r["module"] == "外部数据源"][0]
        self.assertTrue(row["ok"])
        self.assertIn("休市未发起外部请求", row["detail"])
        self.assertEqual(telemetry.snapshot(), {}, "休市不该产生任何上游请求统计")


# ------------------------------------------------------------------ 3 / 4 / 5

class TestTradingSessionFailures(Base):
    SESSION = ("交易中", True)

    def _boom(self, exc):
        def fake(url, **kw):
            raise exc
        return fake

    def test_3_timeout_without_cache_returns_none(self):
        with mock.patch.object(d.requests, "get", side_effect=self._boom(real_requests.Timeout("t"))):
            self.assertIsNone(d.get_market_flow(), "抓取失败不能伪装成零值行情")
        prov = telemetry.snapshot()
        self.assertEqual(prov["tencent_quote"]["calls"], 1)
        self.assertEqual(prov["tencent_quote"]["failed"], 1)
        self.assertIn("Timeout", prov["tencent_quote"]["last_error"])

    def test_3b_timeout_with_cache_degrades_and_marks_stale(self):
        self.seed("market_flow", self.market_data(), age_sec=1800)   # 30 分钟前，超过 15 分钟
        with mock.patch.object(d.requests, "get", side_effect=self._boom(real_requests.Timeout("t"))):
            out = d.get_market_flow()
        self.assertIsNotNone(out)
        self.assertTrue(out["_stale"], "降级数据必须标 stale")
        self.assertEqual(out["index_price"], 3649.18)

    def test_4_empty_payload_is_empty_not_failed(self):
        with mock.patch.object(d.requests, "get", side_effect=lambda url, **kw: Resp("")):
            self.assertIsNone(d.get_market_flow())
        prov = telemetry.snapshot()["tencent_quote"]
        self.assertEqual(prov["empty"], 1, "空报文记 empty")
        self.assertEqual(prov["failed"], 0, "空报文不该记 failed")

    def test_5_real_zero_flows_are_kept(self):
        """东财明确返回全 0：这是真实数据，不能判失败。"""
        def fake(url, **kw):
            if "qt.gtimg" in url:
                return Resp(index_text())
            return Resp(flow_payload(ZERO_FLOW))
        with mock.patch.object(d.requests, "get", side_effect=fake):
            out = d.get_market_flow()
        self.assertIsNotNone(out, "真实零值被当成失败了")
        self.assertEqual(out["main_force"], 0)
        self.assertEqual(out["super_large"], 0)
        self.assertTrue(out["_flow_available"], "必须记住'源确实给了记录'")
        self.assertFalse(out["_stale"])
        self.assertEqual(out["date"], "20261106")
        # 落盘后下次直接命中，零值不会被当成空数据丢掉
        self.assertIn("market_flow", d._load_cache())

    def test_5b_zero_flows_without_source_records_still_invalid(self):
        """源什么都没给（diff 为空）：不能拿零值冒充行情。"""
        def fake(url, **kw):
            if "qt.gtimg" in url:
                return Resp(index_text())
            return Resp(json.dumps({"data": {"diff": []}}))
        with mock.patch.object(d.requests, "get", side_effect=fake):
            out = d.get_market_flow()
        self.assertIsNone(out, "没有资金记录时不能组装成行情（零值是造出来的）")

    def test_valid_data_accepts_explicit_zero(self):
        zeros = {"main_force": 0, "super_large": 0, "large_order": 0,
                 "middle_order": 0, "small_order": 0, "date": "20261106",
                 "index_price": 3649.18, "_flow_available": True}
        self.assertTrue(d._has_valid_data(dict(zeros)), "真实零值必须算有效数据")
        self.assertFalse(d._has_valid_data(dict(zeros, _flow_available=False)),
                         "没拿到记录的全零不能算有效")
        self.assertFalse(d._has_valid_data(dict(zeros, index_price=0)),
                         "没有指数价格不能算有效")


# ------------------------------------------------------------------ 6

class StubProvider(Provider):
    kind = "test"

    def __init__(self, name, status="success", data=None):
        self.name = name
        self.timeout = 1.0
        self._status = status
        self._data = data if data is not None else {"v": 1}

    def request(self, *a, **kw):
        return "raw"

    def normalize(self, raw, *a, **kw):
        from services.data.exceptions import ProviderEmpty, ProviderError
        if self._status == "failed":
            raise ProviderError(self.name, "stub 失败")
        if self._status == "empty":
            raise ProviderEmpty(self.name, "stub 空")
        return self._data, ""


class TestChainFallback(Base):
    SESSION = ("交易中", True)

    def setUp(self):
        super().setUp()
        self._saved = dict(registry._REGISTRY)

    def tearDown(self):
        registry._REGISTRY.clear()
        registry._REGISTRY.update(self._saved)
        super().tearDown()

    def test_6_primary_fails_secondary_succeeds(self):
        registry.register(StubProvider("bad", status="failed"), replace=True)
        registry.register(StubProvider("good", data={"v": 2}), replace=True)
        r = registry.call_chain(["bad", "good"])
        self.assertTrue(r.ok())
        self.assertEqual(r.source, "good")
        self.assertEqual(r.data, {"v": 2})
        self.assertEqual([a["status"] for a in r.attempts], ["failed", "success"])

    def test_6b_all_fail_keeps_attempts(self):
        registry.register(StubProvider("bad1", status="failed"), replace=True)
        registry.register(StubProvider("bad2", status="failed"), replace=True)
        r = registry.call_chain(["bad1", "bad2"])
        self.assertFalse(r.ok())
        self.assertEqual(len(r.attempts), 2)


# ------------------------------------------------------------------ 遥测衔接

class TestTelemetryWiring(Base):

    def test_cache_hit_does_not_count_as_upstream_call(self):
        SESSION = ("交易中", True)
        d._market_status = lambda: SESSION
        calls = {"n": 0}

        def fake(url, **kw):
            if "qt.gtimg" in url:
                calls["n"] += 1
                return Resp(index_text())
            return Resp(flow_payload())

        with mock.patch.object(d.requests, "get", side_effect=fake):
            d.get_market_flow()          # 第一次：真实抓取
            before = telemetry.snapshot()["tencent_quote"]["calls"]
            d.get_market_flow()          # 第二次：内存命中，不该再打上游
        after = telemetry.snapshot()["tencent_quote"]["calls"]
        self.assertEqual(before, after, "缓存命中被重复计成上游请求了")
        cs = d.cache_stats()
        self.assertGreaterEqual(cs["hit"], 1)
        self.assertEqual(cs["miss"], 1)

    def test_closed_session_does_not_touch_upstream(self):
        self.seed("market_flow", self.market_data(), age_sec=2 * 86400)
        with mock.patch.object(d.requests, "get",
                               side_effect=AssertionError("休市不许联网")):
            out = d.get_market_flow()
        self.assertEqual(out["date"], "20261106")
        self.assertEqual(telemetry.snapshot(), {}, "休市不该有上游请求")
        self.assertGreaterEqual(d.cache_stats()["closed_served"], 1)

    def test_provider_error_types_are_distinguished(self):
        def boom(url, **kw):
            raise real_requests.ConnectionError("conn reset")
        with mock.patch.object(tp.requests, "get", side_effect=boom):
            registry.call("tencent_quote", "600519")
        st = telemetry.snapshot()["tencent_quote"]
        self.assertEqual(st["failed"], 1)
        self.assertIn("NetworkError", st["last_error"])
        self.assertEqual(st["last_status"], "failed")

    def test_cache_and_provider_stats_are_separate_dimensions(self):
        SESSION = ("交易中", True)
        d._market_status = lambda: SESSION

        def fake(url, **kw):
            if "qt.gtimg" in url:
                return Resp(index_text())
            return Resp(flow_payload())
        with mock.patch.object(d.requests, "get", side_effect=fake):
            d.get_market_flow()
        self.assertIn("market_flow", d.source_stats(), "缓存键维度统计")
        self.assertIn("tencent_quote", d.provider_stats(), "Provider 维度统计")
        self.assertNotIn("tencent_quote", d.source_stats())


if __name__ == "__main__":
    unittest.main()
