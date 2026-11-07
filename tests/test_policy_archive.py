# -*- coding: utf-8 -*-
"""政策事件回退逻辑回归测试（2026-11 V2.0.1 P1）

关键语义：DB 可用但该股票没有有效事件（包括事件被归档）时，必须返回空列表，
不能回退到静态字典 —— 否则归档操作在估值页面上"无效"，老事件会复活。

三种状态：
  unavailable → 回退静态字典（页面别空白）
  empty       → 回退静态字典（首次部署还没导入数据）
  ok          → 只用 DB 结果，哪怕 []
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import events_store            # noqa: E402
import policy_notes            # noqa: E402


class PolicyFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "events.db")
        self._patch = mock.patch.object(events_store, "db_path", return_value=self.db)
        self._patch.start()
        self.conn = events_store.connect()
        events_store.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self.tmp.cleanup()

    def _add(self, title="测试事件", code="000657", status="active"):
        eid, _, _ = events_store.add_event(
            "2026-10-01", title, "摘要", "利好", "测试来源",
            stocks=(code,), status=status, conn=self.conn)
        self.conn.commit()
        return eid

    def test_db_state_transitions(self):
        self.assertEqual(events_store.db_state(self.conn), "empty")
        self._add()
        self.assertEqual(events_store.db_state(self.conn), "ok")

    def test_empty_db_falls_back_to_static(self):
        """还没 seed 过：静态字典兜底，页面不该空白。"""
        notes = policy_notes.get_policy_notes("000657", "小金属")
        self.assertTrue(notes)
        self.assertEqual(notes[0]["date"], "2026-08-27")

    def test_db_event_replaces_static(self):
        self._add("真实录入的事件")
        notes = policy_notes.get_policy_notes("000657", "小金属")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["title"], "真实录入的事件")

    def test_archived_all_events_returns_empty_not_static(self):
        """归档后必须真的消失；如果回退静态字典，归档等于没做。"""
        self._add("会被归档的事件")
        self.assertTrue(policy_notes.get_policy_notes("000657", "小金属"))

        self.conn.execute("UPDATE events SET status='archived'")
        self.conn.commit()

        notes = policy_notes.get_policy_notes("000657", "小金属")
        self.assertEqual(notes, [])

    def test_archived_does_not_hide_active(self):
        old = self._add("旧事件")
        self.conn.execute("UPDATE events SET status='archived' WHERE id=?", (old,))
        self._add("新事件")
        self.conn.commit()
        notes = policy_notes.get_policy_notes("000657", "小金属")
        self.assertEqual([n["title"] for n in notes], ["新事件"])

    def test_db_unavailable_falls_back(self):
        with mock.patch.object(events_store, "db_state", return_value="unavailable"):
            notes = policy_notes.get_policy_notes("000657", "小金属")
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main()
