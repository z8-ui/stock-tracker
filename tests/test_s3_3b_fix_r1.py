# -*- coding: utf-8 -*-
"""S3-3B r1 复审整改：三项必改的回归测试（全部离线）。

对应 GPT 复审：
  必改 1  旧元数据可能被错误复用（同内容新一轮 / schema 不支持 / fetched_at 对不上）
  必改 2  写盘失败导致响应与 Headers 不对应
  必改 3  旧 JSON is_stale 与新 Headers 状态矛盾
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

from tests.test_s3_3b_sector_cache import Resp, sector_http, legacy_rows  # noqa: E402


def flow_rows(n=5):
    return [{"name": f"板块{i}", "change": 1.0 + i, "flow": (i - 2) * 1.5}
            for i in range(n)]


def meta_for(rows, ts, completeness="complete", total=None, schema=1, **kw):
    total = len(rows) if total is None else total
    rec = {
        "schema": schema, "cache_key": "sector_flow", "mode": "flow",
        "source": "eastmoney_sector", "fetch_status": "success",
        "completeness": completeness, "rows": len(rows), "total": total,
        "missing_pages": [], "missing": [], "notes": [], "duplicates": 0,
        "fetched_at": float(ts), "asof": "", "asof_source": "unknown",
        "is_mock": False, "data_hash": d._fingerprint(rows),
    }
    rec.update(kw)
    return rec


class R1Base(unittest.TestCase):
    SECTOR = "sector_flow"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = d._CACHE_FILE
        self._status = d._market_status
        self._last = dict(d._LAST_SUCCESS)
        d._CACHE_FILE = os.path.join(self.tmp.name, "cache.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._LEGACY_MIGRATION_AT.clear()
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

    def seed(self, key, value, fetched_at=None, meta=None):
        cache = self.read_cache()
        cache[key] = {"__data": value, "fetched_at": fetched_at}
        if meta is not None:
            cache.setdefault(d._META_NAMESPACE, {})[key] = meta
        self.write_cache(cache)

    def restart(self):
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._LEGACY_MIGRATION_AT.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()

    def assert_stale_consistent(self, r, body):
        """JSON is_stale 必须与 X-Data-Freshness 同源（必改 3 的核心不变量）。"""
        header_stale = r.headers["X-Data-Freshness"] != "fresh"
        self.assertEqual(bool(body.get("is_stale")), header_stale,
                         "JSON is_stale 与 X-Data-Freshness 矛盾")


class TestMetaReuse(R1Base):
    """必改 1：旧元数据不能被错误复用。"""

    def test_01_same_content_new_round_meta_failure_drops_old_meta(self):
        """内容完全相同、只是新一轮抓取：meta 生成失败后旧 complete 必须失效。"""
        rows = flow_rows(5)
        ts = time.time() - 120
        self.seed(self.SECTOR, rows, fetched_at=ts, meta=meta_for(rows, ts))
        self.restart()

        def boom(value, ts2):
            raise RuntimeError("meta builder crashed")

        with self.http(sector_http(total=5)):
            out = d._cached_get(self.SECTOR, lambda: rows, validate_fn=d._validate_sectors,
                                ttl=60, meta_fn=boom)
        self.assertEqual(out, rows)
        cache = self.read_cache()
        self.assertNotIn(self.SECTOR, cache.get(d._META_NAMESPACE) or {},
                         "元数据生成失败后旧元数据仍在，会被继续当成 complete")

        meta = d.data_meta(self.SECTOR, served_from="network", data=out)
        self.assertEqual(meta["completeness"], "unknown")
        self.assertIn(meta["meta_status"], ("missing", "not_persisted"))
        self.assertNotEqual(meta["completeness"], "complete")

    def test_02_fetched_at_mismatch_is_untrusted(self):
        """GPT 复现：数据时间已更新、元数据还是旧时间、hash 相同 → 不得 ok。"""
        rows = flow_rows(5)
        new_ts = time.time()
        old_ts = new_ts - 3600
        self.seed(self.SECTOR, rows, fetched_at=new_ts,
                  meta=meta_for(rows, old_ts))          # 元数据描述的是上一轮

        meta = d.data_meta(self.SECTOR, served_from="file", data=rows)
        self.assertEqual(meta["meta_status"], "fetched_at")
        self.assertEqual(meta["completeness"], "unknown")
        self.assertIn("meta:fetched_at_mismatch", meta["missing"])

    def test_03_unsupported_schema_is_untrusted(self):
        rows = flow_rows(5)
        ts = time.time()
        self.seed(self.SECTOR, rows, fetched_at=ts,
                  meta=meta_for(rows, ts, schema=999))
        meta = d.data_meta(self.SECTOR, served_from="file", data=rows)
        self.assertEqual(meta["meta_status"], "schema")
        self.assertEqual(meta["completeness"], "unknown",
                         "不认识的 schema 被当成了可信元数据")
        self.assertIn("meta:schema_unsupported", meta["missing"])

    def test_04_invalidated_meta_never_returns_complete_after_restart(self):
        rows = flow_rows(5)
        ts = time.time() - 60
        self.seed(self.SECTOR, rows, fetched_at=ts, meta=meta_for(rows, ts))
        self.restart()
        # 直接删掉元数据（模拟失效/丢失），数据仍在
        cache = self.read_cache()
        cache[d._META_NAMESPACE].pop(self.SECTOR, None)
        self.write_cache(cache)
        self.restart()
        d._market_status = lambda: ("周末休市", False)
        with self.http(lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t"))):
            r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Completeness"], "unknown")
        self.assertNotEqual(r.headers["X-Data-Completeness"], "complete")
        self.assertIs(r.status_code, 200)

    def test_05_legacy_migration_uses_same_trust_rule(self):
        """schema 不支持时，迁移判断也必须认为元数据不可信（不能只看 hash）。"""
        rows = flow_rows(5)
        ts = time.time()
        self.seed(self.SECTOR, rows, fetched_at=ts,
                  meta=meta_for(rows, ts, schema=999))
        d._LEGACY_MIGRATION_AT.clear()
        self.assertTrue(d._needs_legacy_migration(self.SECTOR),
                        "schema 不支持时迁移没有触发，旧元数据会被长期复用")

        # fetched_at 对不上 → 同样需要迁移
        self.seed(self.SECTOR, rows, fetched_at=ts, meta=meta_for(rows, ts - 3600))
        d._LEGACY_MIGRATION_AT.clear()
        self.assertTrue(d._needs_legacy_migration(self.SECTOR))

        # 可信元数据 → 不迁移
        self.seed(self.SECTOR, rows, fetched_at=ts, meta=meta_for(rows, ts))
        d._LEGACY_MIGRATION_AT.clear()
        self.assertFalse(d._needs_legacy_migration(self.SECTOR))

    def test_06_route_meta_failure_is_unknown_not_complete(self):
        """整链路：先有一份 complete，再抓一轮 partial 且元数据生成失败 → 必须 unknown。"""
        with self.http(sector_http(total=496)):
            self.get("/api/heatmap")                       # 落一份 complete
        self.restart()
        with mock.patch.object(d, "_sector_meta_from", side_effect=RuntimeError("boom")):
            with self.http(sector_http(total=496, fail_pages={5})):
                r, body = self.get("/api/heatmap")
        self.assertEqual(body["code"], 200)
        self.assertEqual(r.headers["X-Data-Completeness"], "unknown",
                         "元数据生成失败却仍报旧的 complete")
        self.assertIn(r.headers["X-Data-Meta-Status"], ("missing", "not_persisted"))


class TestWriteFailure(R1Base):
    """必改 2：写盘失败时响应与 Headers 必须一致。"""

    def test_10_normal_write_is_reported_persisted(self):
        trace = {}
        rows = flow_rows(4)
        d._cached_get(self.SECTOR, lambda: rows, validate_fn=d._validate_sectors,
                      ttl=60, trace=trace,
                      meta_fn=lambda v, ts: meta_for(v, ts))
        self.assertIs(trace.get("persisted"), True)
        meta = d.data_meta(self.SECTOR, served_from="network", trace=trace, data=rows)
        self.assertEqual(meta["meta_status"], "ok")
        self.assertEqual(meta["completeness"], "complete")

    def test_11_save_failure_returns_false_and_keeps_old_file(self):
        old = flow_rows(3)
        ts = time.time() - 60
        self.seed(self.SECTOR, old, fetched_at=ts, meta=meta_for(old, ts))
        with open(d._CACHE_FILE, "rb") as f:
            before = f.read()

        with mock.patch.object(d.os, "replace", side_effect=OSError("disk full")):
            ok = d._cache_update(self.SECTOR, flow_rows(9), time.time(),
                                 meta=meta_for(flow_rows(9), time.time()))

        self.assertIs(ok, False, "_save_cache/_cache_update 没有把写盘失败暴露出来")
        with open(d._CACHE_FILE, "rb") as f:
            self.assertEqual(f.read(), before, "写盘失败破坏了已有缓存文件")

    def test_12_write_failure_headers_describe_returned_data(self):
        """GPT 复现：返回 NEW、磁盘 OLD，Headers 不得继续引用 OLD 的 complete。"""
        old = legacy_rows(3)              # 旧缓存：3 行
        ts = time.time() - 60
        self.seed(self.SECTOR, old, fetched_at=ts, meta=meta_for(old, ts))
        self.restart()

        with mock.patch.object(d, "_save_cache", lambda c: False):
            with self.http(sector_http(total=496)):
                r, body = self.get("/api/heatmap")

        self.assertEqual(body["code"], 200)
        self.assertEqual(len(body["data"]), 496, "返回的不是本次新数据")
        h = r.headers
        self.assertEqual(h["X-Data-Served-From"], "network")
        self.assertEqual(h["X-Data-Meta-Status"], "not_persisted")
        self.assertEqual(h["X-Data-Completeness"], "unknown",
                         "写盘失败却沿用了磁盘上旧数据的 complete")
        self.assertEqual(h["X-Data-Rows"], "496", "行数必须按本次返回的数据报")
        # 磁盘仍是旧数据（没有被写坏）
        disk = self.read_cache()[self.SECTOR]["__data"]
        self.assertEqual(len(disk), 3)

    def test_13_write_recovers_and_meta_becomes_trusted(self):
        old = legacy_rows(3)
        ts = time.time() - 60
        self.seed(self.SECTOR, old, fetched_at=ts, meta=meta_for(old, ts))
        self.restart()
        with mock.patch.object(d, "_save_cache", lambda c: False):
            with self.http(sector_http(total=496)):
                self.get("/api/heatmap")
        self.restart()                     # 清掉失败那轮的内存数据
        d._LEGACY_MIGRATION_AT.clear()
        with self.http(sector_http(total=496)):
            r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Completeness"], "complete")
        self.assertEqual(r.headers["X-Data-Meta-Status"], "ok")
        self.assertEqual(len(self.read_cache()[self.SECTOR]["__data"]), 496)

    def test_14_failed_write_does_not_touch_other_keys(self):
        old = flow_rows(3)
        ts = time.time() - 60
        self.seed(self.SECTOR, old, fetched_at=ts, meta=meta_for(old, ts))
        cache = self.read_cache()
        cache["market_flow"] = {"__data": {"main_force": 1.0}, "fetched_at": ts}
        self.write_cache(cache)
        with mock.patch.object(d, "_save_cache", lambda c: False):
            d._cache_update(self.SECTOR, flow_rows(9), time.time())
        cache = self.read_cache()
        self.assertEqual(cache["market_flow"]["__data"]["main_force"], 1.0)
        self.assertEqual(len(cache[self.SECTOR]["__data"]), 3)


class TestStaleConsistency(R1Base):
    """必改 3：JSON is_stale 与 X-Data-Freshness 必须同源。"""

    def test_20_closed_market_valid_file_cache_is_fresh(self):
        with self.http(sector_http(total=496)):
            self.get("/api/heatmap")
        self.restart()
        d._market_status = lambda: ("周末休市", False)
        with self.http(lambda url, **kw: (_ for _ in ()).throw(AssertionError("不该重抓"))):
            r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Served-From"], "file")
        self.assertEqual(r.headers["X-Data-Freshness"], "fresh")
        self.assertFalse(body["is_stale"])
        self.assert_stale_consistent(r, body)

    def test_21_closed_market_upstream_failure_is_stale_json(self):
        """旧缓存(无元数据) + 一次安全刷新失败 → 旧 JSON 也必须 is_stale=true。"""
        rows = flow_rows(5)
        self.seed(self.SECTOR, rows, fetched_at=time.time() - 86400)
        self.restart()
        d._market_status = lambda: ("周末休市", False)
        with self.http(lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t"))):
            r, body = self.get("/api/heatmap")
        self.assertEqual(body["code"], 200)
        self.assertEqual(r.headers["X-Data-Served-From"], "file_fallback")
        self.assertEqual(r.headers["X-Data-Freshness"], "stale")
        self.assertTrue(body["is_stale"], "Headers 说 stale，旧 JSON 却说 is_stale=false")
        self.assertIn("上游抓取失败", body["stale_reason"])
        self.assert_stale_consistent(r, body)

    def test_22_trading_upstream_failure_falls_back_and_is_stale(self):
        with self.http(sector_http(total=496)):
            self.get("/api/heatmap")
        self.restart()
        with self.http(lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t"))):
            r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Served-From"], "file_fallback")
        self.assertEqual(r.headers["X-Data-Fetch-Status"], "failed")
        self.assertTrue(body["is_stale"])
        self.assertIn("上游抓取失败", body["stale_reason"])
        self.assert_stale_consistent(r, body)

    def test_23_unknown_metadata_is_stale_not_fresh(self):
        """没有可信元数据 → 不知道是否新鲜，JSON 不能声称 is_stale=false。"""
        self.seed(self.SECTOR, legacy_rows(5), fetched_at=time.time() - 60)
        self.restart()
        d._market_status = lambda: ("周末休市", False)
        with self.http(lambda url, **kw: (_ for _ in ()).throw(real_requests.Timeout("t"))):
            r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Completeness"], "unknown")
        self.assertIn(r.headers["X-Data-Freshness"], ("unknown", "stale"))
        self.assertTrue(body["is_stale"])
        self.assert_stale_consistent(r, body)

    def test_24_write_failure_is_stale_in_json(self):
        """写盘失败 → freshness unknown → JSON 也不得说新鲜。"""
        with mock.patch.object(d, "_save_cache", lambda c: False):
            with self.http(sector_http(total=496)):
                r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Meta-Status"], "not_persisted")
        self.assertTrue(body["is_stale"])
        self.assert_stale_consistent(r, body)


if __name__ == "__main__":
    unittest.main()
