# -*- coding: utf-8 -*-
"""采集候选的「机器状态 × 人工审核」双维度测试（V2.0.1 收尾 P1-5）

核心不变量：
  - 被展示（pending）≠ 被审核；未审核候选永远留在待审核列表
  - AI 抽取失败 = extract_failed，仍然待处理
  - 只有人工 approved / rejected 才能让候选消失
  - 已审核的结论不能被后续采集覆盖
  - 旧库的 shown/skipped 迁移成 pending，不能丢候选
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import event_feed                 # noqa: E402
import events_store               # noqa: E402


def _item(fp, title="候选事件"):
    return {"fp": fp, "source": "东财", "title": title, "url": "http://x"}


class FeedDecisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "events.db")
        self._patch = mock.patch.object(events_store, "db_path", return_value=self.db)
        self._patch.start()
        event_feed.init_seen()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def _row(self, fp):
        conn = events_store.connect()
        row = conn.execute("SELECT * FROM feed_seen WHERE fp=?", (fp,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def test_pending_is_not_reviewed(self):
        """第一次采集/展示：进待审核，但绝不能算“已处理”。"""
        event_feed.mark([_item("a")])
        row = self._row("a")
        self.assertEqual(row["review_status"], "pending")
        self.assertEqual(row["process_status"], "pending")
        self.assertIn("a", event_feed.pending_fp())
        self.assertNotIn("a", event_feed.seen_fps())

    def test_extract_failed_stays_pending(self):
        event_feed.mark([_item("f")], process="extract_failed")
        self.assertIn("f", event_feed.pending_fp())
        self.assertNotIn("f", event_feed.seen_fps())
        self.assertEqual(self._row("f")["process_status"], "extract_failed")

    def test_approved_and_ingested_finish_the_candidate(self):
        event_feed.mark([_item("i")], process="ingested", review="approved", event_id=42)
        row = self._row("i")
        self.assertEqual(row["review_status"], "approved")
        self.assertEqual(row["process_status"], "ingested")
        self.assertEqual(row["event_id"], 42)
        self.assertNotIn("i", event_feed.pending_fp())

    def test_rejected_is_final(self):
        event_feed.mark([_item("r")], review="rejected")
        self.assertEqual(self._row("r")["review_status"], "rejected")
        self.assertNotIn("r", event_feed.pending_fp())
        self.assertIn("r", event_feed.seen_fps())

    def test_repeat_collection_does_not_overwrite_review(self):
        """已人工通过后，第二天重复抓到同一条：审核结论和 event_id 都不能被冲掉。"""
        event_feed.mark([_item("x")], process="ingested", review="approved", event_id=7)
        event_feed.mark([_item("x")])                       # 又“展示”了一次
        row = self._row("x")
        self.assertEqual(row["review_status"], "approved")
        self.assertEqual(row["process_status"], "ingested")
        self.assertEqual(row["event_id"], 7)
        self.assertNotIn("x", event_feed.pending_fp())

    def test_repeat_collection_keeps_rejected(self):
        event_feed.mark([_item("y")], review="rejected")
        event_feed.mark([_item("y")])
        self.assertEqual(self._row("y")["review_status"], "rejected")

    def test_duplicate_collection_creates_single_row(self):
        for _ in range(3):
            event_feed.mark([_item("d")])
        conn = events_store.connect()
        n = conn.execute("SELECT COUNT(*) FROM feed_seen WHERE fp='d'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_legacy_shown_migrates_to_pending(self):
        """旧库里的 shown/skipped 是机器批量标记，迁移后必须重新变成待审核。"""
        conn = events_store.connect()
        conn.execute(
            "INSERT INTO feed_seen (fp,source,title,url,first_seen,decision,event_id) "
            "VALUES ('old','东财','旧候选','', '2026-11-01 08:00:00','shown',NULL)")
        conn.execute(
            "INSERT INTO feed_seen (fp,source,title,url,first_seen,decision,event_id) "
            "VALUES ('skip','东财','旧跳过','', '2026-11-01 08:00:00','skipped',NULL)")
        conn.commit()
        conn.close()

        event_feed.init_seen()                    # 触发迁移
        self.assertEqual(self._row("old")["review_status"], "pending")
        self.assertEqual(self._row("skip")["review_status"], "pending")
        self.assertIn("old", event_feed.pending_fp())
        self.assertIn("skip", event_feed.pending_fp())

    def test_stats_split_review_and_process(self):
        event_feed.mark([_item("p1")])
        event_feed.mark([_item("p2")], process="extract_failed")
        event_feed.mark([_item("p3")], review="rejected")
        event_feed.mark([_item("p4")], process="ingested", review="approved", event_id=1)
        stats = event_feed.decision_stats()
        self.assertEqual(stats["review"]["pending"], 2)
        self.assertEqual(stats["review"]["rejected"], 1)
        self.assertEqual(stats["review"]["approved"], 1)
        self.assertEqual(stats["process"]["extract_failed"], 1)
        self.assertEqual(stats["process"]["ingested"], 1)


if __name__ == "__main__":
    unittest.main()
