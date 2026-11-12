# -*- coding: utf-8 -*-
"""S3-4B：F10 请求级财报状态（trace）+ 接口透传 专项测试。

原则：
  - 全部离线：真实 S3-0 报文回放，`_providers.call` 与行情/行业 PE 全部 mock
  - 跑的是生产函数：get_latest_financials(trace=...) / get_stock_valuation /
    estimate_intrinsic_value / Flask 路由，不复制状态判断逻辑
  - 并发用 threading.Barrier 做同步点，不靠 sleep 碰运气
  - 状态对象按本次请求生成：纯缓存命中必须是 not_attempted，不得继承上一轮 failed
"""

import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import data_service as d                                              # noqa: E402
from services.data.base import ProviderResult                         # noqa: E402
from services.data.providers.eastmoney_f10 import (                    # noqa: E402
    EastMoneyF10Provider, PROVIDER_NAME,
)

RAW_PATH = os.path.join(ROOT, "tests", "fixtures", "s3_0", "em_f10_600519.raw")

STATUS_KEYS = {"fetch_status", "served_from", "freshness",
               "fetched_at", "report_date", "cache_rejected", "reason"}


def load_raw():
    with open(RAW_PATH, encoding="utf-8") as f:
        return json.load(f)


def normalized(code="600519"):
    data, _asof, _extra = EastMoneyF10Provider().normalize(load_raw(), code)
    return data


def ok_result(code="600519", payload=None):
    data = payload if payload is not None else normalized(code)
    return ProviderResult(data, PROVIDER_NAME, "success", data.get("report_date"),
                          0.0, False, {}, {})


def empty_result():
    return ProviderResult(None, PROVIDER_NAME, "empty", "", 0.0, False,
                          {"message": "result.data=[]"}, {})


def failed_result(msg="upstream unreachable"):
    return ProviderResult(None, PROVIDER_NAME, "failed", "", 0.0, False,
                          {"message": msg}, {})


QUOTE = {"code": "600519", "name": "贵州茅台", "price": 1500.0, "pe": 30.0,
         "change_pct": 1.2, "volume": 1000, "amount": 1500000.0,
         "_asof": time.time(), "_source": "tencent"}


class StatusTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.remove(self.tmp.name)
        self._orig_file = d._CACHE_FILE
        d._CACHE_FILE = self.tmp.name
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._FIN_LAST_DIAG.clear()
        self.calls = []

    def tearDown(self):
        d._CACHE_FILE = self._orig_file
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._FIN_LAST_DIAG.clear()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    # ---- 工具 ----
    def provider(self, result):
        def call(name, code, *a, **kw):
            self.calls.append((name, code))
            return result() if callable(result) else result
        return mock.patch.object(d._providers, "call", side_effect=call)

    def closed(self):
        return mock.patch.object(d, "_market_status", return_value=("已收盘", False))

    def trading(self):
        return mock.patch.object(d, "_market_status", return_value=("交易中", True))

    def preload_file(self, code, data, age=0):
        # 落盘格式与 _store() 一致：{"__data": 数据, "fetched_at": 抓取时间}
        payload = {f"fin_{code}": {"__data": data, "fetched_at": time.time() - age}}
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def preload_memory(self, code, data, age=0):
        d._IN_MEMORY_CACHE[f"fin_{code}"] = {
            "data": data, "time": time.time() - age, "source": "eastmoney", "ttl": 6 * 3600}

    def assert_status_contract(self, status):
        self.assertEqual(set(status.keys()), STATUS_KEYS)
        self.assertIn(status["fetch_status"], {"success", "empty", "failed", "not_attempted"})
        self.assertIn(status["served_from"], {"network", "memory", "file",
                                              "file_fallback", "memory_fallback", "none"})
        self.assertIn(status["freshness"], {"fresh", "stale", "unknown"})
        self.assertIsInstance(status["cache_rejected"], bool)
        self.assertIsInstance(status["reason"], str)


class TestGroupA_StatusMatrix(StatusTestBase):
    """A：状态矩阵 1~12 —— 每种来源/成败都单独表达。"""

    def test_network_success(self):
        trace = {}
        with self.provider(ok_result()), self.trading():
            fin = d.get_latest_financials("600519", trace=trace)
        st = trace["status"]
        self.assert_status_contract(st)
        self.assertEqual(fin["eps"], 51.53)
        self.assertEqual(st["fetch_status"], "success")
        self.assertEqual(st["served_from"], "network")
        self.assertEqual(st["freshness"], "fresh")
        self.assertEqual(st["report_date"], "2026-09-30")
        self.assertIsNotNone(st["fetched_at"])
        self.assertFalse(st["cache_rejected"])

    def test_empty_without_cache(self):
        trace = {}
        with self.provider(empty_result()), self.trading():
            fin = d.get_latest_financials("600519", trace=trace)
        st = trace["status"]
        self.assert_status_contract(st)
        self.assertIsNone(fin, "没有财报就是 None，不能造零值财报")
        self.assertEqual(st["fetch_status"], "empty")
        self.assertEqual(st["served_from"], "none")
        self.assertIsNone(st["report_date"])
        self.assertIsNone(st["fetched_at"])
        self.assertEqual(st["freshness"], "unknown")

    def test_failed_without_cache(self):
        trace = {}
        with self.provider(failed_result("timeout")), self.trading():
            fin = d.get_latest_financials("600519", trace=trace)
        st = trace["status"]
        self.assertIsNone(fin)
        self.assertEqual(st["fetch_status"], "failed")
        self.assertEqual(st["served_from"], "none")
        self.assertIn("timeout", st["reason"])

    def test_memory_cache_hit_is_not_attempted(self):
        self.preload_memory("600519", normalized())
        trace = {}
        with self.provider(failed_result()), self.trading():
            fin = d.get_latest_financials("600519", trace=trace)
        st = trace["status"]
        self.assertEqual(fin["eps"], 51.53)
        self.assertEqual(self.calls, [], "有效内存缓存不该请求上游")
        self.assertEqual(st["fetch_status"], "not_attempted")
        self.assertEqual(st["served_from"], "memory")
        self.assertEqual(st["freshness"], "fresh")

    def test_file_cache_hit_is_not_attempted(self):
        self.preload_file("600519", normalized())
        trace = {}
        with self.provider(failed_result()), self.closed():
            fin = d.get_latest_financials("600519", trace=trace)
        st = trace["status"]
        self.assertEqual(fin["eps"], 51.53)
        self.assertEqual(self.calls, [])
        self.assertEqual(st["fetch_status"], "not_attempted")
        self.assertEqual(st["served_from"], "file")

    def test_force_refresh_success(self):
        self.preload_file("600519", dict(normalized(), eps=40.0))
        trace = {}
        with self.provider(ok_result()), self.closed():
            fin = d.get_latest_financials("600519", refresh=True, trace=trace)
        st = trace["status"]
        self.assertEqual(fin["eps"], 51.53, "refresh=True 必须穿透缓存")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(st["fetch_status"], "success")
        self.assertEqual(st["served_from"], "network")

    def test_force_refresh_failure_falls_back_to_file(self):
        self.preload_file("600519", normalized())
        trace = {}
        with self.provider(failed_result()), self.closed():
            fin = d.get_latest_financials("600519", refresh=True, trace=trace)
        st = trace["status"]
        self.assertEqual(fin["eps"], 51.53)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(st["fetch_status"], "failed", "回退不能写成 success")
        self.assertEqual(st["served_from"], "file_fallback")
        self.assertEqual(st["freshness"], "stale")

    def test_force_refresh_failure_falls_back_to_memory(self):
        self.preload_memory("600519", normalized(), age=3600)
        trace = {}
        with self.provider(failed_result()), self.trading():
            fin = d.get_latest_financials("600519", refresh=True, trace=trace)
        st = trace["status"]
        self.assertEqual(fin["eps"], 51.53)
        self.assertEqual(st["fetch_status"], "failed")
        self.assertIn(st["served_from"], {"file_fallback", "memory_fallback"})
        self.assertEqual(st["freshness"], "stale")

    def test_bad_cache_then_upstream_success(self):
        bad = dict(normalized(), eps=float("inf"))
        self.preload_file("600519", bad)
        trace = {}
        with self.provider(ok_result()), self.closed():
            fin = d.get_latest_financials("600519", trace=trace)
        st = trace["status"]
        self.assertEqual(fin["eps"], 51.53)
        self.assertTrue(st["cache_rejected"])
        self.assertEqual(st["served_from"], "network")
        self.assertEqual(st["fetch_status"], "success")

    def test_bad_cache_then_upstream_failure(self):
        self.preload_file("600519", dict(normalized(), eps=float("nan")))
        trace = {}
        with self.provider(failed_result()), self.closed():
            fin = d.get_latest_financials("600519", trace=trace)
        st = trace["status"]
        self.assertIsNone(fin)
        self.assertTrue(st["cache_rejected"])
        self.assertEqual(st["fetch_status"], "failed")
        self.assertEqual(st["served_from"], "none")
        json.dumps(st, allow_nan=False)          # 状态本身也必须可严格序列化

    def test_cache_hit_does_not_inherit_previous_failure(self):
        # 第一轮：上游失败，无缓存 → failed
        with self.provider(failed_result()), self.trading():
            t1 = {}
            d.get_latest_financials("600519", trace=t1)
        self.assertEqual(t1["status"]["fetch_status"], "failed")
        # 第二轮：外部写入合法缓存，纯命中、不请求上游 → 必须是 not_attempted
        self.preload_memory("600519", normalized())
        with self.provider(failed_result()), self.trading():
            t2 = {}
            d.get_latest_financials("600519", trace=t2)
        self.assertEqual(t2["status"]["fetch_status"], "not_attempted")
        self.assertEqual(t2["status"]["served_from"], "memory")
        self.assertNotEqual(t2["status"]["reason"], t1["status"]["reason"])

    def test_empty_is_not_written_as_failed(self):
        trace = {}
        with self.provider(empty_result()), self.trading():
            d.get_latest_financials("600519", trace=trace)
        self.assertEqual(trace["status"]["fetch_status"], "empty")
        self.assertEqual(d.get_financials_diagnostic("600519").get("status"), "empty")

    def test_report_date_never_replaced_by_fetched_at(self):
        trace = {}
        with self.provider(ok_result()), self.trading():
            fin = d.get_latest_financials("600519", trace=trace)
        st = trace["status"]
        self.assertEqual(st["report_date"], "2026-09-30")
        self.assertNotEqual(st["fetched_at"][:10], st["report_date"])
        self.assertEqual(fin["report_date"], st["report_date"])

    def test_zero_and_none_still_valid(self):
        payload = dict(normalized(), eps=0.0, roe=None)
        payload["notice_date"] = None
        trace = {}
        with self.provider(ok_result(payload=payload)), self.trading():
            fin = d.get_latest_financials("600519", trace=trace)
        st = trace["status"]
        self.assertEqual(fin["eps"], 0.0)
        self.assertIsNone(fin["roe"])
        self.assertIsNone(fin["notice_date"])
        self.assertEqual(st["fetch_status"], "success")

    def test_without_trace_behaviour_unchanged(self):
        with self.provider(ok_result()), self.trading():
            fin = d.get_latest_financials("600519")
        self.assertEqual(fin["eps"], 51.53)
        self.assertNotIn("status", fin, "状态不能塞进财报本体")


class TestGroupB_Isolation(StatusTestBase):
    """B：并发/多股票隔离（13、14）。"""

    def test_two_requests_same_stock_do_not_share_status(self):
        barrier = threading.Barrier(2, timeout=5)
        results = {}
        lock = threading.Lock()

        def call(name, code, *a, **kw):
            me = threading.current_thread().name
            with lock:
                self.calls.append((me, name, code))
            barrier.wait()
            return ok_result() if me == "winner" else failed_result("boom")

        traces = {}

        def run(thread_name):
            traces[thread_name] = {}
            with mock.patch.object(d._providers, "call", side_effect=call), \
                 mock.patch.object(d, "_market_status", return_value=("交易中", True)):
                d.get_latest_financials("600519", refresh=True, trace=traces[thread_name])

        threads = [threading.Thread(target=run, args=(n,), name=n) for n in ("winner", "loser")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(traces["winner"]["status"]["fetch_status"], "success")
        self.assertEqual(traces["winner"]["status"]["served_from"], "network")
        self.assertEqual(traces["loser"]["status"]["fetch_status"], "failed")
        self.assertNotEqual(traces["loser"]["status"]["served_from"], "network")
        self.assertNotEqual(traces["winner"]["status"], traces["loser"]["status"])

    def test_two_stocks_do_not_share_status(self):
        barrier = threading.Barrier(2, timeout=5)
        good = normalized("600519")

        def call(name, code, *a, **kw):
            barrier.wait()
            return ok_result(payload=good) if code == "600519" else failed_result("000001 down")

        traces = {}
        stocks = ("600519", "000001")

        def run(code):
            traces[code] = {}
            with mock.patch.object(d._providers, "call", side_effect=call), \
                 mock.patch.object(d, "_market_status", return_value=("交易中", True)):
                d.get_latest_financials(code, trace=traces[code])

        threads = [threading.Thread(target=run, args=(c,)) for c in stocks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(traces["600519"]["status"]["report_date"], "2026-09-30")
        self.assertEqual(traces["600519"]["status"]["fetch_status"], "success")
        self.assertIsNone(traces["000001"]["status"]["report_date"])
        self.assertEqual(traces["000001"]["status"]["fetch_status"], "failed")
        self.assertNotEqual(traces["600519"]["status"], traces["000001"]["status"])


class TestGroupC_ValuationAndRoutes(StatusTestBase):
    """C：估值/内在价值业务契约与 HTTP 透传（17）。"""

    def test_get_stock_valuation_carries_status(self):
        trace = {}
        with self.provider(ok_result()), self.trading(), \
             mock.patch.object(d, "get_stock_quote", return_value=dict(QUOTE)), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒",
                                                                  "source": "eastmoney"}), \
             mock.patch.object(d, "get_policy_notes", return_value=[]):
            data = d.get_stock_valuation("600519", trace=trace)
        self.assertIsNotNone(data)
        self.assertEqual(data["financials"]["eps"], 51.53)
        self.assertEqual(data["financials_status"]["fetch_status"], "success")
        self.assertEqual(trace["financials_status"], data["financials_status"])

    def test_estimate_intrinsic_value_carries_status(self):
        trace = {}
        with self.provider(ok_result()), self.trading(), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒",
                                                                  "source": "eastmoney"}):
            iv = d.estimate_intrinsic_value(dict(QUOTE), trace=trace)
        self.assertEqual(iv["model_params"]["raw_eps"], 51.53)
        self.assertEqual(iv["financials_status"]["served_from"], "network")
        self.assertEqual(trace["financials_status"], iv["financials_status"])
        json.dumps(iv, allow_nan=False)

    def test_estimate_intrinsic_value_empty(self):
        with self.provider(empty_result()), self.trading():
            iv = d.estimate_intrinsic_value(dict(QUOTE))
        self.assertFalse(iv["available"])
        self.assertEqual(iv["financials_status"]["fetch_status"], "empty")
        self.assertIsNone(iv["financials_status"]["report_date"])


try:
    from flask import Flask
    import routes as routes_mod
    HAS_FLASK = True
except Exception:                                    # pragma: no cover
    HAS_FLASK = False


@unittest.skipUnless(HAS_FLASK, "当前环境没有 Flask，无法跑 HTTP 透传用例")
class TestGroupD_HttpPassThrough(StatusTestBase):
    """D：/api/valuation 与 /api/intrinsic-value 各自带请求级状态。"""

    def setUp(self):
        super().setUp()
        self.app = Flask(__name__)
        self.app.register_blueprint(routes_mod.bp)
        self.client = self.app.test_client()

    def test_valuation_api_returns_status(self):
        with self.provider(ok_result()), self.trading(), \
             mock.patch.object(d, "get_stock_quote", return_value=dict(QUOTE)), \
             mock.patch.object(routes_mod, "get_industry_pe",
                               return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney",
                                             "source_label": "", "is_static": False}), \
             mock.patch.object(d, "get_industry_pe",
                               return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney"}), \
             mock.patch.object(d, "get_policy_notes", return_value=[]):
            resp = self.client.get("/api/valuation?code=600519")
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["code"], 200)
        chart = body["data"]
        self.assertIn("financials", chart)
        self.assertEqual(chart["financials"]["report_date"], "2026-09-30")
        st = chart["financials_status"]
        self.assertEqual(set(st.keys()), STATUS_KEYS)
        self.assertEqual(st["fetch_status"], "success")

    def test_valuation_api_empty_still_200(self):
        with self.provider(empty_result()), self.trading(), \
             mock.patch.object(d, "get_stock_quote", return_value=dict(QUOTE)), \
             mock.patch.object(routes_mod, "get_industry_pe",
                               return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney",
                                             "source_label": "", "is_static": False}), \
             mock.patch.object(d, "get_industry_pe",
                               return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney"}), \
             mock.patch.object(d, "get_policy_notes", return_value=[]):
            resp = self.client.get("/api/valuation?code=600519")
        self.assertEqual(resp.status_code, 200)
        chart = resp.get_json()["data"]
        self.assertIsNone(chart["financials"])
        self.assertEqual(chart["financials_status"]["fetch_status"], "empty")

    def test_intrinsic_api_returns_status(self):
        with self.provider(failed_result()), self.trading(), \
             mock.patch.object(routes_mod, "get_stock_quote", return_value=dict(QUOTE)), \
             mock.patch.object(d, "get_industry_pe",
                               return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney"}):
            resp = self.client.get("/api/intrinsic-value?code=600519")
        self.assertEqual(resp.status_code, 200)
        iv = resp.get_json()["data"]
        self.assertIsNone(iv["financials"])
        self.assertEqual(iv["financials_status"]["fetch_status"], "failed")
        self.assertFalse(iv["available"])

    def test_both_endpoints_same_stock_independent_status(self):
        """估值与内在价值各自请求一次：一个成功、一个失败，状态不互相污染。"""
        state = {"n": 0}

        def call(name, code, *a, **kw):
            state["n"] += 1
            # 第 1 次（/api/valuation）失败，第 2 次（/api/intrinsic-value）成功
            return failed_result("boom") if state["n"] == 1 else ok_result()

        with mock.patch.object(d._providers, "call", side_effect=call), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)), \
             mock.patch.object(d, "get_stock_quote", return_value=dict(QUOTE)), \
             mock.patch.object(routes_mod, "get_stock_quote", return_value=dict(QUOTE)), \
             mock.patch.object(d, "get_industry_pe",
                               return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney"}), \
             mock.patch.object(routes_mod, "get_industry_pe",
                               return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney",
                                             "source_label": "", "is_static": False}), \
             mock.patch.object(d, "get_policy_notes", return_value=[]):
            first = self.client.get("/api/valuation?code=600519").get_json()["data"]
            second = self.client.get("/api/intrinsic-value?code=600519").get_json()["data"]
        self.assertEqual(first["financials_status"]["fetch_status"], "failed")
        self.assertIsNone(first["financials"])
        self.assertEqual(second["financials_status"]["fetch_status"], "success")
        self.assertEqual(second["financials_status"]["served_from"], "network")
        self.assertNotEqual(first["financials_status"], second["financials_status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
