# -*- coding: utf-8 -*-
"""S3-3B r1b 复审整改：后台 SWR 写盘失败 + JSON/Header 新鲜度一致性。

对应 GPT 第二次复审的两项必改：
  必改 1  _spawn_refresh() 先发布内存、后写盘；写盘失败时新 partial 被旧 complete 掩盖
  必改 2  freshness=fresh 时 JSON 仍可能 is_stale=true（_LAST_SUCCESS 是旧时间）

全部离线；后台线程用 Event 同步，不依赖 sleep。
"""

import threading
import time
import unittest
from unittest import mock

from tests.test_s3_3b_fix_r1 import R1Base, flow_rows, meta_for  # noqa: F401
from tests.test_s3_3b_sector_cache import sector_http  # noqa: E402
import data_service as d


class SWRBase(R1Base):
    """在 R1Base 之上加"后台刷新跑完"的同步点。"""

    def run_bg(self, fetcher, meta_fn=None, persist=None, timeout=5.0):
        """触发一次后台刷新，等线程真正结束（_inflight_release 在 finally 里）。

        persist: None=真写盘；callable=用它替代 _cache_update（返回值即写盘结果）。
        """
        fin = threading.Event()
        real_release = d._inflight_release
        real_update = d._cache_update

        def _release(*a, **kw):
            try:
                return real_release(*a, **kw)
            finally:
                fin.set()

        def _update(*a, **kw):
            if persist is not None:
                return persist(*a, **kw)
            return real_update(*a, **kw)

        with mock.patch.object(d, "_inflight_release", side_effect=_release), \
                mock.patch.object(d, "_cache_update", side_effect=_update):
            d._spawn_refresh(self.SECTOR, fetcher, d._validate_sectors, meta_fn=meta_fn)
            self.assertTrue(fin.wait(timeout), "后台刷新没有在 %.0fs 内结束" % timeout)


class TestBackgroundSWR(SWRBase):

    def _seed_complete(self, rows, ts=None):
        ts = time.time() - 60 if ts is None else ts
        d._cache_update(self.SECTOR, rows, ts,
                        meta=meta_for(rows, ts, completeness="complete"))
        with d._CACHE_LOCK:
            d._IN_MEMORY_CACHE[self.SECTOR] = {"data": rows, "time": ts}
            d._LAST_SUCCESS[self.SECTOR] = ts
        return ts

    # 1）正常：写盘成功 → 发布
    def test_01_background_success_publishes(self):
        old = flow_rows(5)
        self._seed_complete(old)
        new = flow_rows(8)
        self.run_bg(lambda: new,
                    meta_fn=lambda v, ts: meta_for(v, ts, completeness="partial", total=99))
        self.assertEqual(len(d._IN_MEMORY_CACHE[self.SECTOR]["data"]), 8)
        rec = d._load_cache()[d._META_NAMESPACE][self.SECTOR]
        self.assertEqual(rec["completeness"], "partial")
        self.assertEqual(rec["rows"], 8)
        meta = d.data_meta(self.SECTOR, served_from="memory", data=new)
        self.assertEqual(meta["completeness"], "partial")
        self.assertEqual(meta["meta_status"], "ok")

    # 2）写盘失败 → 不发布新内存数据
    def test_02_background_persist_failure_does_not_publish(self):
        old = flow_rows(5)
        ts_old = self._seed_complete(old)
        new = flow_rows(8)
        with open(d._CACHE_FILE, "rb") as fh:
            before = fh.read()
        self.run_bg(lambda: new,
                    meta_fn=lambda v, ts: meta_for(v, ts, completeness="partial", total=99),
                    persist=lambda *a, **kw: False)
        self.assertEqual(d._IN_MEMORY_CACHE[self.SECTOR]["data"], old, "写盘失败却发布了新数据")
        self.assertEqual(d._IN_MEMORY_CACHE[self.SECTOR]["time"], ts_old)
        self.assertEqual(d._LAST_SUCCESS[self.SECTOR], ts_old)
        with open(d._CACHE_FILE, "rb") as fh:
            self.assertEqual(fh.read(), before, "磁盘被改坏了")
        meta = d.data_meta(self.SECTOR, served_from="memory", data=old)
        self.assertEqual(meta["completeness"], "complete")
        self.assertLess(abs(float(meta["fetched_at"]) - ts_old), 1e-3, "元数据时间被写成了新一轮")

    # 3）内容完全相同、新一轮 partial，写盘失败 → 不能被标成 complete 的新轮次
    def test_03_same_hash_partial_not_published(self):
        rows = flow_rows(5)
        ts_old = self._seed_complete(rows)
        self.run_bg(lambda: rows,          # 内容一模一样
                    meta_fn=lambda v, ts: meta_for(v, ts, completeness="partial", total=99),
                    persist=lambda *a, **kw: False)
        self.assertEqual(d._LAST_SUCCESS[self.SECTOR], ts_old, "新的 partial 轮次被发布了")
        meta = d.data_meta(self.SECTOR, served_from="memory", data=rows)
        # 服务仍在描述磁盘上那份旧 complete 快照，而不是把新 partial 说成 complete
        self.assertLess(abs(float(meta["fetched_at"]) - ts_old), 1e-3, "新的 partial 轮次覆盖了旧时间")
        self.assertEqual(meta["rows"], 5)

    # 4）失败后重启读文件：仍然描述磁盘上实际持久化的旧数据
    def test_04_file_read_after_failed_refresh_is_old_snapshot(self):
        rows = flow_rows(5)
        ts_old = self._seed_complete(rows, ts=time.time() - 120)
        with mock.patch.object(d, "_market_status", lambda: ("已收盘", False)):
            self.run_bg(lambda: flow_rows(9),
                        meta_fn=lambda v, ts: meta_for(v, ts, completeness="complete"),
                        persist=lambda *a, **kw: False)
            d._IN_MEMORY_CACHE.clear()
            d._LAST_SUCCESS.clear()
            got = d.get_sector_flow(trace={})
        self.assertEqual(len(got), 5, "重启后读到了未落盘的新数据")
        meta = d.data_meta(self.SECTOR, served_from="file")
        self.assertEqual(meta["rows"], 5)
        self.assertEqual(meta["completeness"], "complete")
        self.assertLess(abs(float(meta["fetched_at"]) - ts_old), 1)

    # 5）恢复写盘后可以正常发布并读到可信元数据
    def test_05_recovers_after_persist_works_again(self):
        old = flow_rows(5)
        self._seed_complete(old)
        new = flow_rows(9)
        self.run_bg(lambda: new,
                    meta_fn=lambda v, ts: meta_for(v, ts, completeness="partial", total=99),
                    persist=lambda *a, **kw: False)
        self.assertEqual(len(d._IN_MEMORY_CACHE[self.SECTOR]["data"]), 5)
        self.run_bg(lambda: new,
                    meta_fn=lambda v, ts: meta_for(v, ts, completeness="partial", total=99))
        self.assertEqual(len(d._IN_MEMORY_CACHE[self.SECTOR]["data"]), 9)
        meta = d.data_meta(self.SECTOR, served_from="memory", data=new)
        self.assertEqual(meta["completeness"], "partial")
        self.assertEqual(meta["rows"], 9)

    # 6）没有 meta_fn 的其它 Provider：行为保持原样（写盘失败也发布）
    def test_06_other_providers_unchanged(self):
        """没有 meta_fn 的 Provider 维持原行为：写盘失败也发布内存（不受本轮修改影响）。"""
        old = flow_rows(2)
        with d._CACHE_LOCK:
            d._IN_MEMORY_CACHE[self.SECTOR] = {"data": old, "time": 1.0}
        new = flow_rows(3)
        self.run_bg(lambda: new, meta_fn=None, persist=lambda *a, **kw: False)
        self.assertEqual(d._IN_MEMORY_CACHE[self.SECTOR]["data"], new)

    # 7）后台元数据生成失败：数据落盘但元数据丢弃 → unknown，不冒充 complete
    def test_07_background_meta_failure_drops_meta(self):
        old = flow_rows(5)
        self._seed_complete(old)

        def boom(v, ts):
            raise RuntimeError("meta boom")

        new = flow_rows(7)
        self.run_bg(lambda: new, meta_fn=boom)
        self.assertEqual(len(d._IN_MEMORY_CACHE[self.SECTOR]["data"]), 7)
        cache = d._load_cache()
        self.assertNotIn(self.SECTOR, cache.get(d._META_NAMESPACE) or {})
        meta = d.data_meta(self.SECTOR, served_from="memory", data=new)
        self.assertEqual(meta["meta_status"], "missing")
        self.assertEqual(meta["completeness"], "unknown")


class TestStaleConsistencyFresh(SWRBase):
    """freshness 与 JSON is_stale 必须同源（必改 2）。"""

    def _seed(self, rows, ts, meta=True):
        self.seed(self.SECTOR, rows, fetched_at=ts,
                  meta=(meta_for(rows, ts, completeness="complete") if meta else None))

    def test_10_fresh_meta_beats_stale_last_success(self):
        """外部进程更新过缓存文件：_LAST_SUCCESS 很旧，但元数据是新的。"""
        rows = flow_rows(5)
        now = time.time()
        self._seed(rows, now)
        with d._CACHE_LOCK:
            d._LAST_SUCCESS[self.SECTOR] = now - 86400      # 进程内时间还是旧的
        with mock.patch.object(d, "_market_status", lambda: ("已收盘", False)), \
                self.http(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))):
            r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Freshness"], "fresh")
        self.assertFalse(body["is_stale"], "freshness=fresh 却 is_stale=true")
        self.assertEqual(body["stale_reason"], "", "is_stale=false 却带着过期原因")
        self.assertTrue(body["asof"].startswith(time.strftime("%Y-%m-%d")))

    def test_11_stale_meta_is_stale(self):
        rows = flow_rows(5)
        self._seed(rows, time.time() - 6 * 86400)
        with mock.patch.object(d, "_market_status", lambda: ("已收盘", False)), \
                self.http(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))):
            r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Freshness"], "stale")
        self.assertTrue(body["is_stale"])
        self.assertTrue(body["stale_reason"])

    def test_12_missing_meta_never_claims_fresh(self):
        """旧缓存无元数据 + 安全刷新失败 → 保守 stale，且与 Headers 一致。"""
        self.seed(self.SECTOR, flow_rows(5), fetched_at=None, meta=None)
        with mock.patch.object(d, "_market_status", lambda: ("已收盘", False)), \
                self.http(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))):
            r, body = self.get("/api/heatmap")
        self.assertIn(r.headers["X-Data-Freshness"], ("stale", "unknown"))
        self.assertTrue(body["is_stale"])
        self.assert_stale_consistent(r, body)

    def test_12b_unknown_freshness_is_stale(self):
        """写盘失败 → freshness=unknown（不知道有多新），JSON 必须保守 stale。"""
        rows = flow_rows(5)
        ts = time.time() - 60
        self.seed(self.SECTOR, rows, fetched_at=ts,
                  meta=meta_for(rows, ts, completeness="complete"))
        self.restart()
        with mock.patch.object(d, "_market_status", lambda: ("交易中", True)), \
                mock.patch.object(d, "_save_cache", lambda cache: False), \
                self.http(sector_http(total=6)):
            r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Meta-Status"], "not_persisted")
        self.assertEqual(r.headers["X-Data-Freshness"], "unknown")
        self.assertTrue(body["is_stale"])
        self.assert_stale_consistent(r, body)

    def test_13_file_fallback_is_stale(self):
        rows = flow_rows(5)
        self._seed(rows, time.time() - 60)
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with mock.patch.object(d, "_market_status", lambda: ("交易中", True)), \
                self.http(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))):
            r, body = self.get("/api/heatmap")
        self.assertEqual(r.headers["X-Data-Served-From"], "file_fallback")
        self.assertTrue(body["is_stale"])
        self.assertIn("上游抓取失败", body["stale_reason"])
        self.assert_stale_consistent(r, body)

    def test_14_all_cases_consistent(self):
        """三种 freshness 都不出现相反判断。"""
        cases = [(time.time(), "fresh", False),
                 (time.time() - 6 * 86400, "stale", True)]
        for ts, want_fresh, want_stale in cases:
            self._seed(flow_rows(5), ts)
            with mock.patch.object(d, "_market_status", lambda: ("已收盘", False)), \
                    self.http(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))):
                r, body = self.get("/api/heatmap")
            self.assertEqual(r.headers["X-Data-Freshness"], want_fresh)
            self.assertEqual(bool(body["is_stale"]), want_stale)


if __name__ == "__main__":
    unittest.main()
