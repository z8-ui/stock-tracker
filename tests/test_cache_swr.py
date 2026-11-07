# -*- coding: utf-8 -*-
"""缓存层回归测试（2026-11 V2.0.1 P1）

覆盖：
  1. 不同 cache_key 并发写不互相覆盖（读-改-写必须在同一把锁内）
  2. SWR 后台刷新写出的文件结构与 _store 一致（__data + fetched_at）
  3. 重启后能从文件缓存恢复真实抓取时间，不把旧数据标成"刚刚"
  4. 同一 key 并发只构建一次（single-flight）
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import data_service as d          # noqa: E402


class CacheTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_file = d._CACHE_FILE
        self._old_status = d._market_status
        d._CACHE_FILE = os.path.join(self.tmp.name, "cache.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        d._market_status = lambda: ("交易中", True)

    def tearDown(self):
        d._CACHE_FILE = self._old_file
        d._market_status = self._old_status
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        self.tmp.cleanup()

    def _read_file(self):
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)


class TestConcurrentWrites(CacheTestBase):
    def test_different_keys_do_not_overwrite_each_other(self):
        """20 个线程同时写不同 key，最后一个都不能丢。

        旧实现是「请求开始时读整份文件 → 结束时写整份」，两个请求交错就会
        用旧快照覆盖掉对方刚写的 key（lost update）。
        """
        n = 20
        barrier = threading.Barrier(n)
        errors = []

        def worker(i):
            try:
                barrier.wait(5)
                d._cache_update(f"k{i}", {"i": i}, time.time())
            except Exception as e:      # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(errors, [])
        cache = self._read_file()
        missing = [f"k{i}" for i in range(n) if f"k{i}" not in cache]
        self.assertEqual(missing, [], f"并发写丢了这些 key：{missing}")
        for i in range(n):
            self.assertEqual(cache[f"k{i}"]["__data"], {"i": i})
            self.assertIn("fetched_at", cache[f"k{i}"])

    def test_cache_update_keeps_other_keys(self):
        d._cache_update("a", {"v": 1}, 1000.0)
        d._cache_update("b", {"v": 2}, 2000.0)
        d._cache_update("a", {"v": 3}, 3000.0)
        cache = self._read_file()
        self.assertEqual(cache["a"]["__data"], {"v": 3})
        self.assertEqual(cache["a"]["fetched_at"], 3000.0)
        self.assertEqual(cache["b"]["__data"], {"v": 2})


class TestSWR(CacheTestBase):
    def test_background_refresh_writes_same_structure(self):
        old_ts = time.time() - 3600
        d._IN_MEMORY_CACHE["k"] = {"data": {"v": 1}, "time": old_ts}

        def fetcher():
            return {"v": 2}

        got = d._cached_get("k", fetcher, ttl=5)
        # SWR：本次请求立即返回旧值（不能阻塞在网络上）
        self.assertEqual(got["v"], 1)
        self.assertAlmostEqual(got["_asof"], old_ts, places=1)

        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.exists(d._CACHE_FILE):
                cache = self._read_file()
                if cache.get("k", {}).get("__data") == {"v": 2}:
                    break
            time.sleep(0.02)
        else:
            self.fail("后台刷新没有把新值写进文件缓存")

        entry = self._read_file()["k"]
        self.assertIn("__data", entry, "后台刷新写成了裸数据，重启后时间戳会丢")
        self.assertIn("fetched_at", entry)
        self.assertGreater(entry["fetched_at"], old_ts)
        self.assertEqual(d._IN_MEMORY_CACHE["k"]["data"], {"v": 2})

    def test_inflight_flag_cleared_after_refresh(self):
        d._IN_MEMORY_CACHE["k"] = {"data": {"v": 1}, "time": time.time() - 100}
        done = threading.Event()

        def fetcher():
            return {"v": 2}

        d._cached_get("k", fetcher, ttl=5)
        deadline = time.time() + 5
        while time.time() < deadline and "k" in d._IN_FLIGHT:
            time.sleep(0.02)
        self.assertNotIn("k", d._IN_FLIGHT, "_IN_FLIGHT 没有清理，之后永远不再刷新")

    def test_failed_refresh_keeps_old_data(self):
        old_ts = time.time() - 100
        d._IN_MEMORY_CACHE["k"] = {"data": {"v": 1}, "time": old_ts}

        def boom():
            raise RuntimeError("network down")

        got = d._cached_get("k", boom, ttl=5)
        self.assertEqual(got["v"], 1)
        time.sleep(0.2)
        self.assertNotIn("k", d._IN_FLIGHT)
        self.assertEqual(d._IN_MEMORY_CACHE["k"]["data"], {"v": 1})


class TestRestartRecovery(CacheTestBase):
    def test_fetched_at_survives_restart_and_is_marked_stale(self):
        ts = time.time() - 5 * 86400      # 五天前抓的，已经不能代表"最近一次收盘"
        d._cache_update("k", {"v": 9}, ts)

        # 模拟进程重启：内存清空，只剩文件
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._market_status = lambda: ("已收盘", False)

        got = d._cached_get("k", lambda: None, ttl=5)
        self.assertEqual(got["v"], 9)
        self.assertAlmostEqual(got["_asof"], ts, places=1)
        self.assertTrue(got["_asof_known"])
        self.assertTrue(got["_stale"], "五天前的缓存没有被标记为陈旧")
        self.assertIn("分钟未更新", got["_stale_reason"])
        self.assertAlmostEqual(d._LAST_SUCCESS["k"], ts, places=1)

    def test_recent_close_snapshot_is_not_stale(self):
        """休盘时两小时前的收盘快照是正常的，不该天天报"陈旧"。"""
        ts = time.time() - 2 * 3600
        d._cache_update("k", {"v": 5}, ts)
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._market_status = lambda: ("已收盘", False)

        got = d._cached_get("k", lambda: {"v": 6}, ttl=5)
        self.assertEqual(got["v"], 5)
        self.assertFalse(got["_stale"])

    def test_legacy_cache_without_timestamp_is_unknown_not_now(self):
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"k": {"v": 7}}, f)
        d._IN_MEMORY_CACHE.clear()
        d._market_status = lambda: ("已收盘", False)

        def fetch_fails():
            return None          # 旧格式缓存无法确认新鲜度 → 先尝试重抓，失败才退回

        got = d._cached_get("k", fetch_fails, ttl=5)
        self.assertEqual(got["v"], 7)
        self.assertIsNone(got["_asof"])
        self.assertFalse(got["_asof_known"])
        self.assertTrue(got["_stale"], "抓取时间未知的旧缓存必须标记为陈旧")

    def test_stale_legacy_cache_is_refreshed_when_possible(self):
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"k": {"v": 7}}, f)
        d._IN_MEMORY_CACHE.clear()
        d._market_status = lambda: ("已收盘", False)

        got = d._cached_get("k", lambda: {"v": 8}, ttl=5)
        self.assertEqual(got["v"], 8, "陈旧缓存应被一次真实抓取替换")


class TestSingleFlight(CacheTestBase):
    def test_concurrent_miss_builds_once(self):
        calls = []
        lock = threading.Lock()

        def fetcher():
            with lock:
                calls.append(1)
            time.sleep(0.05)
            return {"v": "built"}

        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait(5)
            results.append(d._cached_get("same", fetcher, ttl=60)["v"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(len(calls), 1, f"同一 key 被并发构建了 {len(calls)} 次")
        self.assertEqual(results, ["built"] * 8)


class TestSWRInvalidResults(CacheTestBase):
    """P1：后台刷新不能把有效缓存刷成 None / 空 / 校验失败的数据。

    原实现 `validate_fn is None or validate_fn(fresh)` 在 validate_fn=None 时
    对 None 放行，一次失败的后台抓取就能把内存 + 文件 + _LAST_SUCCESS 一起刷空。
    """

    def _seed(self):
        self.old_ts = time.time() - 100
        self.old = {"v": 1}
        d._IN_MEMORY_CACHE["k"] = {"data": self.old, "time": self.old_ts}
        d._cache_update("k", self.old, self.old_ts)

    def _wait_done(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline and "k" in d._IN_FLIGHT:
            time.sleep(0.02)
        self.assertNotIn("k", d._IN_FLIGHT, "后台刷新没有清理 _IN_FLIGHT")

    def _assert_kept(self):
        self.assertEqual(d._IN_MEMORY_CACHE["k"]["data"], self.old, "内存缓存被刷坏")
        self.assertAlmostEqual(d._IN_MEMORY_CACHE["k"]["time"], self.old_ts, places=3)
        self.assertEqual(self._read_file()["k"]["__data"], self.old, "文件缓存被覆盖")

    def test_none_result_keeps_both_caches(self):
        self._seed()
        d._cached_get("k", lambda: None, ttl=5)
        self._wait_done()
        self._assert_kept()

    def test_empty_dict_does_not_overwrite_valid_cache(self):
        self._seed()
        d._cached_get("k", lambda: {}, ttl=5)
        self._wait_done()
        self._assert_kept()

    def test_exception_keeps_both_caches(self):
        self._seed()

        def boom():
            raise RuntimeError("network down")

        d._cached_get("k", boom, ttl=5)
        self._wait_done()
        self._assert_kept()

    def test_failed_validation_keeps_both_caches(self):
        self._seed()
        d._cached_get("k", lambda: {"v": 0}, ttl=5, validate_fn=lambda x: x.get("v", 0) > 0)
        self._wait_done()
        self._assert_kept()

    def test_no_old_cache_and_none_result_writes_nothing(self):
        d._spawn_refresh("k", lambda: None, None)
        self._wait_done()
        self.assertNotIn("k", d._IN_MEMORY_CACHE, "没有旧缓存时把 None 也存进了内存")
        self.assertFalse(os.path.exists(d._CACHE_FILE) and "k" in self._read_file(),
                         "没有旧缓存时把 None 也写进了文件")

    def test_successful_refresh_replaces_cache(self):
        self._seed()
        d._cached_get("k", lambda: {"v": 2}, ttl=5)
        self._wait_done()
        self.assertEqual(d._IN_MEMORY_CACHE["k"]["data"], {"v": 2})
        self.assertEqual(self._read_file()["k"]["__data"], {"v": 2})

    def test_concurrent_refresh_of_same_key_runs_once(self):
        self._seed()
        calls = []
        lock = threading.Lock()

        def fetcher():
            with lock:
                calls.append(1)
            time.sleep(0.05)
            return {"v": 2}

        threads = [threading.Thread(target=lambda: d._cached_get("k", fetcher, ttl=5))
                   for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self._wait_done()
        self.assertEqual(len(calls), 1, f"同一 key 被并发刷新了 {len(calls)} 次")
        self.assertEqual(d._IN_MEMORY_CACHE["k"]["data"], {"v": 2})


if __name__ == "__main__":
    unittest.main()
