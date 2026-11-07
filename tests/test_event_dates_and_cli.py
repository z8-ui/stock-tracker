# -*- coding: utf-8 -*-
"""政策事件的三个时间字段 + 采集 CLI 退出码（V2.0.1 收尾 P1-3/P1-6）

1) occurred_at / published_at / collected_at 严格分离；未知就是 None，绝不补今天
2) 所有新闻源失败 → CLI 非零退出；没有新候选但来源成功 → 0
3) 抽取失败：候选保留为 extract_failed，下次还能看到
"""

import argparse
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import event_feed                 # noqa: E402
import events_store               # noqa: E402
import events_cli                 # noqa: E402


class DateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "events.db")
        self._p = mock.patch.object(events_store, "db_path", return_value=self.db)
        self._p.start()
        events_store.init_db()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def _poster(self, payload):
        def _p(url, headers, body, timeout):
            return {"choices": [{"message": {"content": __import__("json").dumps(payload)}}]}
        return _p

    def xtract(self, payload, **kw):
        return events_store.extract_event("商务部公告……", api_key="TESTKEY",
                                          poster=self._poster(payload), **kw)[0]


class TestExtractDates(DateCase):
    def test_both_dates_present(self):
        ev = self.xtract({"occurred_at": "2026-10-01", "published_at": "2026-09-28 09:00",
                          "title": "T"})
        self.assertEqual(ev["occurred_at"], "2026-10-01")
        self.assertEqual(ev["published_at"], "2026-09-28 09:00")
        self.assertNotEqual(ev["occurred_at"], ev["published_at"])

    def test_only_published(self):
        ev = self.xtract({"occurred_at": None, "published_at": "2026-09-28",
                          "title": "T"})
        self.assertIsNone(ev["occurred_at"])
        self.assertEqual(ev["published_at"], "2026-09-28")

    def test_no_date_at_all(self):
        ev = self.xtract({"title": "T"})
        self.assertIsNone(ev["occurred_at"])
        self.assertIsNone(ev["published_at"])
        self.assertIsNotNone(ev["collected_at"])

    def test_null_and_empty_and_invalid(self):
        for bad in (None, "", "  ", "2026-13-40", "今天", "2026/10/01"):
            ev = self.xtract({"occurred_at": bad, "title": "T"})
            self.assertIsNone(ev["occurred_at"], bad)
            ev2 = self.xtract({"date": bad, "title": "T"})
            self.assertIsNone(ev2["occurred_at"], bad)

    def test_today_param_does_not_fill_unknown(self):
        """today 参数只是兼容位：传了也不能变成事件日期。"""
        ev = events_store.extract_event(
            "x", api_key="TESTKEY", today="2026-11-07",
            poster=self._poster({"title": "T"}))[0]
        self.assertIsNone(ev["occurred_at"])

    def test_published_hint_used_but_not_as_occurrence(self):
        ev = events_store.extract_event(
            "x", api_key="TESTKEY", published_at="2026-11-06 08:30",
            poster=self._poster({"title": "T"}))[0]
        self.assertEqual(ev["published_at"], "2026-11-06 08:30")
        self.assertIsNone(ev["occurred_at"])


class TestStoreDates(DateCase):
    def test_unknown_date_stored_as_null_not_today(self):
        eid, created, _ = events_store.add_event(title="未知日期事件", published_at="2026-11-01 09:00")
        self.assertTrue(created)
        conn = events_store.connect()
        row = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        conn.close()
        self.assertIsNone(row["occurred_at"])
        self.assertEqual(row["event_date"], "")       # NOT NULL 兼容列也不许塞今天
        self.assertEqual(row["published_at"], "2026-11-01 09:00")
        self.assertIsNotNone(row["collected_at"])

    def test_collected_differs_from_published(self):
        eid, _, _ = events_store.add_event(title="时间三兄弟", occurred_at="2026-10-01",
                                           published_at="2026-11-05 10:00",
                                           collected_at="2026-11-06 23:59:00")
        conn = events_store.connect()
        row = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        conn.close()
        self.assertEqual(row["occurred_at"], "2026-10-01")
        self.assertEqual(row["published_at"], "2026-11-05 10:00")
        self.assertEqual(row["collected_at"], "2026-11-06 23:59")

    def test_dates_survive_restart(self):
        eid, _, _ = events_store.add_event(title="重启不变", occurred_at="2026-10-02",
                                           published_at="2026-11-05 10:00")
        # 模拟重启：新连接重新读库（不依赖任何进程内缓存）
        conn = events_store.connect()
        row = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        conn.close()
        self.assertEqual(row["occurred_at"], "2026-10-02")
        self.assertEqual(row["published_at"], "2026-11-05 10:00")

    def test_notes_expose_both_times(self):
        events_store.add_event(title="估值页事件", occurred_at=None,
                               published_at="2026-11-01 09:00",
                               stocks=["600519"])
        notes = events_store.notes_for("600519")
        self.assertEqual(notes[0]["occurred_at"], None)
        self.assertEqual(notes[0]["published_at"], "2026-11-01 09:00")
        self.assertEqual(notes[0]["date"], None)
        self.assertFalse(notes[0]["date_known"])


class TestFeedCliExit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "events.db")
        self._p = mock.patch.object(events_store, "db_path", return_value=self.db)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def _args(self, **kw):
        base = dict(source=None, limit=50, keyword=None, all=False, strong=False,
                    dry_run=False, pick=None, reject=None, stock=None,
                    industry=None, yes=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def _run(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = events_cli.cmd_feed(args)
        return rc, out.getvalue(), err.getvalue()

    def test_all_sources_failed_returns_nonzero(self):
        with mock.patch.object(event_feed, "fetch_headlines", return_value=([], ["x: timeout"], 0)):
            rc, _, err = self._run(self._args())
        self.assertEqual(rc, 3)
        self.assertIn("所有新闻源", err)

    def test_partial_failure_returns_zero(self):
        items = [{"title": "钨出口管制", "source": "东财", "source_key": "eastmoney",
                  "summary": "", "url": "", "published_at": "2026-11-06 09:00"}]
        with mock.patch.object(event_feed, "fetch_headlines", return_value=(items, ["sina: timeout"], 1)):
            rc, out, _ = self._run(self._args())
        self.assertEqual(rc, 0)
        self.assertIn("1 个源成功", _)

    def test_success_with_no_candidates_returns_zero(self):
        with mock.patch.object(event_feed, "fetch_headlines", return_value=([], [], 2)):
            rc, out, _ = self._run(self._args())
        self.assertEqual(rc, 0)
        self.assertIn("没有新候选", out)

    def test_extract_failure_returns_one_and_keeps_candidate(self):
        items = [{"title": "钨出口管制", "source": "东财", "source_key": "eastmoney",
                  "summary": "", "url": "", "published_at": "2026-11-06 09:00"}]
        with mock.patch.object(event_feed, "fetch_headlines", return_value=(items, [], 2)), \
                mock.patch.object(events_store, "extract_event", side_effect=RuntimeError("boom")):
            rc, _, err = self._run(self._args(pick="1", yes=True))
        self.assertEqual(rc, 1)
        self.assertEqual(event_feed.decision_stats()["process"].get("extract_failed"), 1)
        # 下次还能出现
        fp = list(event_feed.pending_fp())[0]
        self.assertTrue(fp)


if __name__ == "__main__":
    unittest.main()
