# -*- coding: utf-8 -*-
"""S3-3B：板块缓存旁路元数据 + HTTP Headers（全部离线，不打真实上游）

覆盖 GPT 要求的 16 类场景：
  1  496/496 正常抓取            2  400/496 部分成功
  3  某一分页失败                 4  重复板块导致数量不匹配
  5  total 跨页不一致             6  内存缓存命中
  7  文件缓存命中                 8  进程重启后读旁路元数据
  9  旧 60 行缓存                 10 5 行合法测试缓存
  11 元数据缺失                   12 元数据 hash 不匹配
  13 上游失败后旧缓存回退         14 完全无缓存且上游失败
  15 估值 PB 496/496 null 但完整  16 负 PE 90 个保留，不误判为缺失

做法：mock 最外层 `data_service.requests.get`（Provider 内部同样是 requests.get，
所以分页循环 / 去重 / partial 判定都真跑），再用 Flask test_client 断言
最终 HTTP 响应头 + JSON 主体结构。
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


class Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status
        self.encoding = "utf-8"


def sector_http(total=496, fail_pages=(), page_total_shift=None, duplicate_pages=(),
                pe_negative=0, page_size=100, calls=None):
    """假的东财 clist 分页接口（按 pn/pz 返回真实分页形态）。"""
    page_total_shift = page_total_shift or {}

    def rows_for(start, count):
        rows = []
        for i in range(start, start + count):
            pe = -5.0 if i < pe_negative else 10.0 + (i % 50)
            rows.append({
                "f12": f"BK{i:04d}", "f14": f"板块{i}",
                "f3": 1.0 + (i % 5), "f62": ((i % 7) - 3) * 1e8,
                "f9": pe, "f23": "-", "f20": 1e10 + i * 1e6,
            })
        return rows

    def fn(url, **kw):
        params = kw.get("params") or {}
        pn = int(params.get("pn", 1))
        pz = int(params.get("pz", page_size))
        if calls is not None:
            calls.append(pn)
        assert "clist/get" in url, f"未预期的请求: {url}"
        if pn in fail_pages:
            return Resp("", status=502)
        if pn > 1 and (pn - 1) in duplicate_pages:
            # 这一页返回上一页的内容 → 去重后行数对不上 total
            start = max(0, (pn - 2) * pz)
            return Resp(json.dumps({"rc": 0, "data": {
                "total": total, "diff": rows_for(start, min(pz, total - start))}}))
        start = (pn - 1) * pz
        count = max(0, min(pz, total - start))
        if count == 0:
            return Resp(json.dumps({"rc": 0, "data": None}))     # 越界页
        page_total = total + page_total_shift.get(pn, 0)
        return Resp(json.dumps({"rc": 0, "data": {
            "total": page_total, "diff": rows_for(start, count)}}))

    return fn


def timeout_http(msg="不该访问上游"):
    def fn(url, **kw):
        raise real_requests.Timeout(msg)
    return fn


def legacy_rows(n=60):
    """升级前 sector_flow 里存的东西：裸 list，没有信封、没有元数据。"""
    return [{"name": f"旧板块{i}", "change": 1.0 + i * 0.1, "flow": (i - 30) * 0.5}
            for i in range(n)]


class SectorCacheTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = d._CACHE_FILE
        self._status = d._market_status
        self._last = dict(d._LAST_SUCCESS)
        d._CACHE_FILE = os.path.join(self.tmp.name, "cache.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._LEGACY_MIGRATION_AT.clear()
        d._SECTOR_LAST_RESULT.clear()
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
        d._LEGACY_MIGRATION_AT.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        self.tmp.cleanup()

    # ---- 工具 ----
    def http(self, fn):
        return mock.patch.object(d.requests, "get", side_effect=fn)

    def get(self, path):
        r = self.client.get(path)
        return r, r.get_json()

    def read_cache(self):
        if not os.path.exists(d._CACHE_FILE):
            return {}
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)

    def write_cache(self, cache):
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    def seed(self, key, value, fetched_at=None, raw=False):
        cache = self.read_cache()
        cache[key] = value if raw else {"__data": value, "fetched_at": fetched_at}
        self.write_cache(cache)

    def meta_of(self, key="sector_flow"):
        return (self.read_cache().get("__data_meta__") or {}).get(key) or {}

    def restart(self):
        """模拟进程重启：内存与 _LAST_SUCCESS 全清，只剩磁盘。"""
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._LEGACY_MIGRATION_AT.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()


class TestNormalAndPartial(SectorCacheTestBase):
    """场景 1/2/3/4/5 + JSON 结构不变。"""

    def test_01_complete_496(self):
        with self.http(sector_http(total=496)):
            r, body = self.get("/api/heatmap")
        self.assertEqual(body["code"], 200)
        self.assertEqual(len(body["data"]), 496)
        # JSON 结构不变：仍是原来的 7 个键，data 是 list
        self.assertEqual(set(body), {"code", "data", "_market_status", "is_mock",
                                     "is_stale", "asof", "stale_reason"})
        self.assertIsInstance(body["data"], list)
        h = r.headers
        self.assertEqual(h["X-Data-Fetch-Status"], "success")
        self.assertEqual(h["X-Data-Completeness"], "complete")
        self.assertEqual(h["X-Data-Freshness"], "fresh")
        self.assertEqual(h["X-Data-Meta-Status"], "ok")
        self.assertEqual(h["X-Data-Rows"], "496")
        self.assertEqual(h["X-Data-Total"], "496")
        self.assertEqual(h["X-Data-Served-From"], "network")
        self.assertEqual(h["X-Data-Missing"], "-")
        self.assertEqual(h["X-Data-AsOf"], "unknown", "上游没给时间时不得伪造 asof")
        self.assertIn("X-Data-Completeness", h["Access-Control-Expose-Headers"])
        # 业务数据仍是裸 list 信封；元数据在独立命名空间
        cache = self.read_cache()
        self.assertIsInstance(cache["sector_flow"]["__data"], list)
        self.assertEqual(cache["sector_flow"]["__data"][0]["name"], "板块0")
        meta = self.meta_of()
        self.assertEqual(meta["completeness"], "complete")
        self.assertEqual(meta["total"], 496)
        self.assertEqual(meta["data_hash"], d._fingerprint(cache["sector_flow"]["__data"]))

    def test_02_partial_400_of_496_last_page_fails(self):
        with self.http(sector_http(total=496, fail_pages={5})):
            r, body = self.get("/api/heatmap")
        self.assertEqual(body["code"], 200)
        h = r.headers
        self.assertEqual(h["X-Data-Fetch-Status"], "success")   # 抓取本身没抛异常
        self.assertEqual(h["X-Data-Completeness"], "partial")   # 但数据不完整
        self.assertEqual(h["X-Data-Rows"], "400")
        self.assertEqual(h["X-Data-Total"], "496")
        self.assertIn("5", h["X-Data-Missing"].split(","))
        self.assertEqual(len(body["data"]), 400)

    def test_03_partial_middle_page_fails(self):
        with self.http(sector_http(total=496, fail_pages={3})):
            r, _ = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Completeness"], "partial")
        self.assertEqual(r.headers["X-Data-Rows"], "396")       # 496 - 第 3 页 100 条
        self.assertIn("3", r.headers["X-Data-Missing"].split(","))

    def test_04_duplicates_make_it_partial(self):
        with self.http(sector_http(total=496, duplicate_pages={2})):
            r, _ = self.get("/api/heatmap")
        h = r.headers
        self.assertEqual(h["X-Data-Completeness"], "partial")
        self.assertEqual(h["X-Data-Rows"], "396")               # 100 条重复被去重
        self.assertIn("duplicates:100", h["X-Data-Missing"])
        self.assertEqual(self.meta_of()["duplicates"], 100)

    def test_05_total_mismatch_across_pages(self):
        with self.http(sector_http(total=496, page_total_shift={2: 10})):
            r, _ = self.get("/api/heatmap")
        h = r.headers
        self.assertEqual(h["X-Data-Completeness"], "partial")
        self.assertIn("total_mismatch", h["X-Data-Missing"])


class TestCacheLayers(SectorCacheTestBase):
    """场景 6/7/8：partial 不能在缓存层丢失。"""

    def test_06_memory_hit_keeps_complete(self):
        fn = sector_http(total=496)
        with self.http(fn):
            self.get("/api/heatmap")
            r, _ = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Served-From"], "memory")
        self.assertEqual(r.headers["X-Data-Completeness"], "complete")

    def test_06b_memory_hit_keeps_partial(self):
        with self.http(sector_http(total=496, fail_pages={5})):
            self.get("/api/heatmap")
            r, _ = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Served-From"], "memory")
        self.assertEqual(r.headers["X-Data-Completeness"], "partial",
                         "partial 被缓存后不能变成 complete")
        self.assertIn("5", r.headers["X-Data-Missing"].split(","))

    def test_07_file_hit_after_restart_complete(self):
        with self.http(sector_http(total=496)):
            self.get("/api/heatmap")
        self.restart()
        d._market_status = lambda: ("周末休市", False)
        with self.http(timeout_http("休市不该重抓")):
            r, body = self.get("/api/heatmap")
        self.assertEqual(body["code"], 200)
        self.assertEqual(r.headers["X-Data-Served-From"], "file")
        self.assertEqual(r.headers["X-Data-Completeness"], "complete")
        self.assertEqual(r.headers["X-Data-Rows"], "496")

    def test_08_partial_survives_restart(self):
        with self.http(sector_http(total=496, fail_pages={5})):
            self.get("/api/heatmap")
        self.restart()
        d._market_status = lambda: ("周末休市", False)
        with self.http(timeout_http("休市不该重抓")):
            r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Completeness"], "partial",
                         "重启后 partial 被读成了 complete")
        self.assertEqual(r.headers["X-Data-Rows"], "400")
        self.assertEqual(r.headers["X-Data-Total"], "496")
        self.assertIn("5", r.headers["X-Data-Missing"].split(","))
        self.assertEqual(len(body["data"]), 400)

    def test_08b_complete_data_not_marked_partial_by_old_meta(self):
        with self.http(sector_http(total=496, fail_pages={5})):
            self.get("/api/heatmap")                 # 先落一份 partial
        self.restart()
        # 上游恢复 → 新数据 + 新元数据一起覆盖，必须回到 complete
        with self.http(sector_http(total=496)):
            r, _ = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Completeness"], "complete")
        self.assertEqual(self.meta_of()["missing"], [])


class TestLegacyCache(SectorCacheTestBase):
    """场景 9/10/11：旧缓存迁移，不用行数阈值。"""

    def test_09_legacy_60_rows_marked_unknown_not_deleted(self):
        self.seed("sector_flow", legacy_rows(60), raw=True)
        d._market_status = lambda: ("周末休市", False)
        with self.http(timeout_http()):
            r, body = self.get("/api/heatmap")
        self.assertEqual(body["code"], 200)
        self.assertEqual(len(body["data"]), 60, "旧缓存被删了")
        h = r.headers
        self.assertEqual(h["X-Data-Completeness"], "unknown", "旧缓存不能冒充完整")
        self.assertEqual(h["X-Data-Meta-Status"], "missing")
        self.assertEqual(h["X-Data-Rows"], "60")
        self.assertEqual(h["X-Data-Total"], "unknown")
        self.assertEqual(h["X-Data-Fetch-Status"], "failed", "刷新失败必须如实记 failed")
        self.assertEqual(h["X-Data-Freshness"], "stale")

    def test_09b_legacy_60_rows_refreshed_once(self):
        """休市也允许一次安全刷新；成功后变 496 + complete。"""
        self.seed("sector_flow", legacy_rows(60), raw=True)
        d._market_status = lambda: ("周末休市", False)
        with self.http(sector_http(total=496)) as p:
            r, body = self.get("/api/heatmap")
            self.assertEqual(p.call_count, 5, "一次安全刷新应抓满 5 页")
        self.assertEqual(len(body["data"]), 496)
        self.assertEqual(r.headers["X-Data-Completeness"], "complete")
        self.assertEqual(r.headers["X-Data-Meta-Status"], "ok")
        self.assertEqual(len(self.read_cache()["sector_flow"]["__data"]), 496)

    def test_09c_refresh_failure_does_not_repeat_every_request(self):
        self.seed("sector_flow", legacy_rows(60), raw=True)
        d._market_status = lambda: ("周末休市", False)
        with self.http(timeout_http()):
            self.get("/api/heatmap")
            self.get("/api/heatmap")
        # 第二次请求不应再打上游（冷却内）
        calls = []
        with self.http(sector_http(total=496, calls=calls)):
            self.get("/api/heatmap")
        self.assertEqual(calls, [], "旧缓存迁移没有冷却，每次请求都打 5 页上游")

    def test_10_legal_5_rows_not_deleted(self):
        self.seed("sector_flow", legacy_rows(5), raw=True)
        d._market_status = lambda: ("周末休市", False)
        with self.http(timeout_http()):
            r, body = self.get("/api/heatmap")
        self.assertEqual(body["code"], 200)
        self.assertEqual(len(body["data"]), 5, "合法的 5 行测试缓存被删了")
        self.assertEqual(r.headers["X-Data-Completeness"], "unknown")

    def test_10b_legal_5_rows_replaced_by_real_data(self):
        self.seed("sector_flow", legacy_rows(5), raw=True)
        with self.http(sector_http(total=496)):
            r, body = self.get("/api/heatmap")
        self.assertEqual(len(body["data"]), 496)
        self.assertEqual(r.headers["X-Data-Completeness"], "complete")

    def test_11_metadata_missing_but_data_is_new_format(self):
        self.seed("sector_flow", legacy_rows(60), fetched_at=time.time() - 60)
        d._market_status = lambda: ("周末休市", False)
        with self.http(timeout_http()):
            r, _ = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Meta-Status"], "missing")
        self.assertEqual(r.headers["X-Data-Completeness"], "unknown")
        self.assertEqual(r.headers["X-Data-Rows"], "60")
        self.assertIn(r.headers["X-Data-Freshness"], ("unknown", "stale"))


class TestMetaIntegrity(SectorCacheTestBase):
    """场景 12/13/14。"""

    def test_12_hash_mismatch_invalidates_meta(self):
        with self.http(sector_http(total=496)):
            self.get("/api/heatmap")
        cache = self.read_cache()
        cache["sector_flow"]["__data"] = legacy_rows(3)     # 数据被改写，元数据没动
        self.write_cache(cache)
        self.restart()
        d._market_status = lambda: ("周末休市", False)
        with self.http(timeout_http()):
            r, _ = self.get("/api/heatmap")
        h = r.headers
        self.assertEqual(h["X-Data-Meta-Status"], "mismatch")
        self.assertEqual(h["X-Data-Completeness"], "unknown")
        self.assertIn("meta:hash_mismatch", h["X-Data-Missing"])
        self.assertEqual(h["X-Data-Rows"], "3", "行数按当前数据如实报，不用旧元数据")

    def test_13_upstream_failure_falls_back_to_stale(self):
        with self.http(sector_http(total=496)):
            self.get("/api/heatmap")
        self.restart()
        d._market_status = lambda: ("交易中", True)
        with self.http(timeout_http()):
            r, body = self.get("/api/heatmap")
        self.assertEqual(body["code"], 200)
        h = r.headers
        self.assertEqual(h["X-Data-Served-From"], "file_fallback")
        self.assertEqual(h["X-Data-Freshness"], "stale")
        self.assertEqual(h["X-Data-Fetch-Status"], "failed")
        self.assertEqual(h["X-Data-Completeness"], "complete", "旧数据本身是完整的")

    def test_14_no_cache_and_upstream_failure(self):
        d._market_status = lambda: ("交易中", True)
        with self.http(timeout_http()):
            r, body = self.get("/api/heatmap")
        self.assertEqual(body["code"], 500)
        h = r.headers
        self.assertEqual(h["X-Data-Served-From"], "none")
        self.assertEqual(h["X-Data-Fetch-Status"], "failed")
        self.assertEqual(h["X-Data-Completeness"], "unknown")
        self.assertEqual(h["X-Data-Rows"], "unknown")
        self.assertEqual(h["X-Data-Total"], "unknown")


class TestSectorValuation(SectorCacheTestBase):
    """场景 15/16：PB 全 null 仍完整；负 PE 保留。"""

    def test_15_pb_all_null_is_still_complete(self):
        with self.http(sector_http(total=496, pe_negative=90)):
            r, body = self.get("/api/sector-valuation")
        self.assertEqual(body["code"], 200)
        self.assertEqual(set(body), {"code", "data", "is_mock"}, "JSON 结构变了")
        self.assertEqual(len(body["data"]), 496)
        self.assertTrue(all(row["pb"] is None for row in body["data"]),
                        "PB 缺失必须保持 null，不能变 0")
        h = r.headers
        self.assertEqual(h["X-Data-Completeness"], "complete",
                         "PB 全缺是上游口径，不能因此把 496 行判成 partial")
        self.assertEqual(h["X-Data-Rows"], "496")
        self.assertEqual(h["X-Data-Total"], "496")

    def test_16_negative_pe_kept_and_not_missing(self):
        with self.http(sector_http(total=496, pe_negative=90)):
            _, body = self.get("/api/sector-valuation")
        negatives = [row for row in body["data"] if row["pe"] is not None and row["pe"] < 0]
        self.assertEqual(len(negatives), 90, "负 PE 被过滤或改成了绝对值")
        meta = self.meta_of("sector_valuation")
        self.assertIn("negative_pe:90", meta["notes"])
        self.assertIn("pb_unavailable:496", meta["notes"])
        self.assertEqual(meta["missing"], [], "负 PE / PB 缺失不是数据缺失")
        self.assertEqual(meta["completeness"], "complete")

    def test_valuation_second_request_zero_upstream_calls(self):
        with self.http(sector_http(total=496)) as first:
            self.get("/api/sector-valuation")
        self.restart()
        d._market_status = lambda: ("周末休市", False)
        with self.http(sector_http(total=496, calls=[])) as second:
            r, _ = self.get("/api/sector-valuation")
        self.assertEqual(second.call_count, 0, "估值二次请求打了上游")
        self.assertEqual(r.headers["X-Data-Served-From"], "file")
        self.assertEqual(r.headers["X-Data-Completeness"], "complete")

    def test_valuation_partial_kept_through_restart(self):
        with self.http(sector_http(total=496, fail_pages={5})):
            self.get("/api/sector-valuation")
        self.restart()
        d._market_status = lambda: ("周末休市", False)
        with self.http(timeout_http()):
            r, _ = self.get("/api/sector-valuation")
        self.assertEqual(r.headers["X-Data-Completeness"], "partial")
        self.assertEqual(r.headers["X-Data-Rows"], "400")


if __name__ == "__main__":
    unittest.main()
