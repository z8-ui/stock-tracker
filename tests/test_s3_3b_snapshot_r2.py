# -*- coding: utf-8 -*-
"""S3-3B r2 复审整改：同一响应内"数据快照 ↔ 元数据"必须同版本。

GPT 复现的跨请求错配：
  请求 A 拿到旧数据 → 请求 B 把同一份列表以新 complete 落盘 → A 再读 data_meta()
  → 旧代码按磁盘校验，A 被标成 complete。数据 hash 相同，所以 hash 校验挡不住。

修复：_cached_get() 在每条返回路径把"这份数据的 fetched_at"写进 trace["version"]；
data_meta() 用该版本校验元数据，版本不一致 → meta_status=fetched_at、completeness=unknown。

并发场景全部用 threading.Event 控制交错点，不用 sleep。
"""

import threading
import time
import unittest
from unittest import mock

from tests.test_s3_3b_fix_r1 import R1Base, flow_rows, meta_for  # noqa: F401
from tests.test_s3_3b_sector_cache import sector_http  # noqa: E402
import data_service as d


class SnapshotBase(R1Base):

    def seed_pair(self, rows, ts, completeness="partial", total=None):
        """预置一份"数据 + 元数据"，并放进内存缓存。"""
        d._cache_update(self.SECTOR, rows, ts,
                        meta=meta_for(rows, ts, completeness=completeness, total=total))
        with d._CACHE_LOCK:
            d._IN_MEMORY_CACHE[self.SECTOR] = {"data": rows, "time": ts}
            d._LAST_SUCCESS[self.SECTOR] = ts
        return ts

    def update_pair(self, rows, ts, completeness="complete", total=None):
        """模拟另一个请求把同一份内容以新状态落盘。"""
        d._cache_update(self.SECTOR, rows, ts,
                        meta=meta_for(rows, ts, completeness=completeness, total=total))

    def request_paused_before_meta(self, gate, started):
        """在后台线程里跑"取数 → 暂停 → data_meta"，返回 (thread, holder)。"""
        holder = {}

        def run():
            tr = {}
            data = d.get_sector_flow(trace=tr)
            holder["trace"] = tr
            holder["data"] = data
            started.set()
            gate.wait(5)
            holder["meta"] = d.data_meta(self.SECTOR, served_from=tr.get("served"),
                                         trace=tr, data=data)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertTrue(started.wait(5), "请求 A 没能取到数据")
        return t, holder


class TestSnapshotBinding(SnapshotBase):

    # 测试 A：旧 partial + 新 complete（内容相同）→ A 不得被标成 complete
    def test_a_old_partial_not_relabelled_by_new_complete(self):
        rows = flow_rows(5)
        ts_a = time.time() - 120
        self.seed_pair(rows, ts_a, completeness="partial", total=99)

        gate, started = threading.Event(), threading.Event()
        t, holder = self.request_paused_before_meta(gate, started)
        self.assertEqual(holder["trace"]["version"], ts_a)

        ts_b = time.time()
        self.update_pair(rows, ts_b, completeness="complete", total=5)   # 新 complete，内容一样
        gate.set()
        t.join(5)

        meta_a = holder["meta"]
        self.assertEqual(meta_a["meta_status"], "fetched_at", "旧请求拿到了新轮次的元数据")
        self.assertEqual(meta_a["completeness"], "unknown")
        self.assertEqual(meta_a["fetched_at"], ts_a, "fetched_at 必须是被返回那份数据的时间")

        # 之后的新读取：内存里已是 B，正常报告 complete
        with d._CACHE_LOCK:
            d._IN_MEMORY_CACHE[self.SECTOR] = {"data": rows, "time": ts_b}
            d._LAST_SUCCESS[self.SECTOR] = ts_b
        tr_b = {}
        data_b = d.get_sector_flow(trace=tr_b)
        meta_b = d.data_meta(self.SECTOR, served_from=tr_b["served"], trace=tr_b, data=data_b)
        self.assertEqual(meta_b["meta_status"], "ok")
        self.assertEqual(meta_b["completeness"], "complete")
        self.assertEqual(meta_b["fetched_at"], ts_b)

    # 测试 B：旧 complete + 新 partial（内容相同）→ A 不误认新 partial，B 的 partial 保留
    def test_b_old_complete_not_overwritten_by_new_partial(self):
        rows = flow_rows(5)
        ts_a = time.time() - 120
        self.seed_pair(rows, ts_a, completeness="complete", total=5)

        gate, started = threading.Event(), threading.Event()
        t, holder = self.request_paused_before_meta(gate, started)
        self.assertEqual(holder["trace"]["version"], ts_a)

        ts_b = time.time()
        self.update_pair(rows, ts_b, completeness="partial", total=99)
        gate.set()
        t.join(5)

        meta_a = holder["meta"]
        self.assertEqual(meta_a["meta_status"], "fetched_at")
        self.assertEqual(meta_a["completeness"], "unknown")
        self.assertNotEqual(meta_a["completeness"], "partial", "旧请求不该采用新轮次的 partial")

        # B 自己的读取：partial 必须正常保留
        with d._CACHE_LOCK:
            d._IN_MEMORY_CACHE[self.SECTOR] = {"data": rows, "time": ts_b}
            d._LAST_SUCCESS[self.SECTOR] = ts_b
        tr_b = {}
        data_b = d.get_sector_flow(trace=tr_b)
        meta_b = d.data_meta(self.SECTOR, served_from=tr_b["served"], trace=tr_b, data=data_b)
        self.assertEqual(meta_b["meta_status"], "ok")
        self.assertEqual(meta_b["completeness"], "partial")
        self.assertEqual(meta_b["rows"], 5)

    # 版本未知（旧格式缓存，没有 fetched_at）→ 不能冒充 ok
    def test_c_unknown_version_is_not_trusted(self):
        self.seed(self.SECTOR, flow_rows(5), fetched_at=None, meta=None)
        with mock.patch.object(d, "_market_status", lambda: ("已收盘", False)), \
                self.http(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))):
            tr = {}
            data = d.get_sector_flow(trace=tr)
            meta = d.data_meta(self.SECTOR, served_from=tr["served"], trace=tr, data=data)
        self.assertIsNone(tr.get("version"))
        self.assertNotEqual(meta["meta_status"], "ok")
        self.assertEqual(meta["completeness"], "unknown")


class TestNormalPathsUnchanged(SnapshotBase):

    def test_10_network_then_memory_same_version(self):
        with self.http(sector_http(total=496)):
            tr1 = {}
            rows = d.get_sector_flow(trace=tr1)
            m1 = d.data_meta(self.SECTOR, served_from=tr1["served"], trace=tr1, data=rows)
            tr2 = {}
            rows2 = d.get_sector_flow(trace=tr2)
            m2 = d.data_meta(self.SECTOR, served_from=tr2["served"], trace=tr2, data=rows2)
        self.assertEqual(tr1["served"], "network")
        self.assertEqual(tr2["served"], "memory")
        self.assertEqual(m1["completeness"], "complete")
        self.assertEqual(m2["completeness"], "complete")
        self.assertEqual(tr1["version"], tr2["version"])
        self.assertEqual(m1["fetched_at"], m2["fetched_at"])

    def test_11_file_hit_after_restart(self):
        with self.http(sector_http(total=496)):
            d.get_sector_flow(trace={})
        self.restart()
        with mock.patch.object(d, "_market_status", lambda: ("已收盘", False)):
            tr = {}
            data = d.get_sector_flow(trace=tr)
            meta = d.data_meta(self.SECTOR, served_from=tr["served"], trace=tr, data=data)
        self.assertEqual(tr["served"], "file")
        self.assertEqual(meta["meta_status"], "ok")
        self.assertEqual(meta["completeness"], "complete")
        self.assertEqual(meta["fetched_at"], tr["version"])

    def test_12_file_fallback_keeps_old_version(self):
        with self.http(sector_http(total=496)):
            d.get_sector_flow(trace={})
        persisted_ts = d._load_cache()[self.SECTOR]["fetched_at"]
        self.restart()
        with mock.patch.object(d, "_market_status", lambda: ("交易中", True)), \
                self.http(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))):
            tr = {}
            data = d.get_sector_flow(trace=tr)
            meta = d.data_meta(self.SECTOR, served_from=tr["served"], trace=tr, data=data)
        self.assertEqual(tr["served"], "file_fallback")
        self.assertEqual(tr["version"], persisted_ts)
        self.assertEqual(meta["meta_status"], "ok")
        self.assertEqual(meta["freshness"], "stale")
        self.assertEqual(meta["completeness"], "complete")
        self.assertEqual(meta["fetch_status"], "failed")

    def test_13_swr_write_failure_keeps_old_version(self):
        rows = flow_rows(5)
        # 60s TTL，喂 120s 前的时间戳 → 命中"过期内存"分支，走 SWR
        ts_old = self.seed_pair(rows, time.time() - 120, completeness="complete", total=5)

        fin = threading.Event()
        real_release = d._inflight_release

        def rel(*a, **kw):
            try:
                return real_release(*a, **kw)
            finally:
                fin.set()

        with mock.patch.object(d, "_cache_update", side_effect=lambda *a, **kw: False), \
                mock.patch.object(d, "_inflight_release", side_effect=rel), \
                self.http(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))):
            tr = {}
            data = d.get_sector_flow(trace=tr)
            meta = d.data_meta(self.SECTOR, served_from=tr["served"], trace=tr, data=data)
            self.assertTrue(fin.wait(5), "后台刷新没有结束")

        self.assertEqual(tr["served"], "stale")
        self.assertEqual(tr["version"], ts_old)
        self.assertEqual(d._IN_MEMORY_CACHE[self.SECTOR]["data"], rows,
                         "写盘失败却发布了新数据")
        self.assertEqual(meta["meta_status"], "ok")
        self.assertEqual(meta["completeness"], "complete")
        self.assertEqual(meta["fetched_at"], ts_old)


if __name__ == "__main__":
    unittest.main()
