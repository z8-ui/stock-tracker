# -*- coding: utf-8 -*-
"""政策事件中心测试（全部离线，用临时 SQLite 文件）

运行：python3 -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events_store  # noqa: E402
import policy_notes  # noqa: E402


class EventStoreCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="events-test-")
        self.db = os.path.join(self.tmpdir, "events.db")
        self._env = mock.patch.dict(os.environ, {"STOCK_EVENTS_DB": self.db})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        for f in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)


class TestWriteRead(EventStoreCase):
    def test_add_then_read(self):
        eid, created, links = events_store.add_event(
            "2026-11-07", "某部委收紧稀土配额", "供给收缩，价格中枢上移",
            "利好", "财联社", "https://example.com/a", stocks=["000657"])
        self.assertTrue(created)
        self.assertEqual(links, 1)

        notes = events_store.notes_for("000657")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["date"], "2026-11-07")
        self.assertEqual(notes[0]["title"], "某部委收紧稀土配额")
        self.assertEqual(notes[0]["impact"], "供给收缩，价格中枢上移")
        self.assertEqual(notes[0]["direction"], "利好")
        self.assertEqual(notes[0]["source"], "财联社")
        self.assertEqual(notes[0]["id"], eid)

    def test_dedupe_same_date_title(self):
        events_store.add_event("2026-11-07", "同一件事", "a", stocks=["000657"])
        eid2, created2, links2 = events_store.add_event(
            "2026-11-07", "同一件事", "a", stocks=["000657", "600549"])
        self.assertFalse(created2)
        self.assertEqual(links2, 1)          # 只补挂了新的那只
        self.assertEqual(events_store.stats()["events"], 1)
        self.assertEqual(len(events_store.notes_for("600549")), 1)

    def test_multi_target_single_row(self):
        events_store.add_event("2026-08-27", "美国禁止废钨出口", "供给收缩", "利好",
                               stocks=["000657", "600549", "002378", "002842"],
                               industries=["小金属"])
        s = events_store.stats()
        self.assertEqual(s["events"], 1)
        self.assertEqual(s["links"], 5)
        for code in ("000657", "600549", "002378", "002842"):
            self.assertEqual(len(events_store.notes_for(code)), 1)
        # 行业关键词包含匹配：小金属Ⅱ 也算命中
        self.assertEqual(len(events_store.notes_for("000001", "小金属Ⅱ")), 1)
        # 不相干行业不命中
        self.assertEqual(events_store.notes_for("000001", "白酒"), [])

    def test_order_and_limit(self):
        events_store.add_event("2026-01-01", "旧事件", stocks=["000657"])
        events_store.add_event("2026-11-07", "新事件", stocks=["000657"])
        notes = events_store.notes_for("000657")
        self.assertEqual([n["title"] for n in notes], ["新事件", "旧事件"])
        rows = events_store.list_events(limit=1, code="000657")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "新事件")

    def test_direction_normalized(self):
        events_store.add_event("2026-11-07", "方向写错的事件", direction="重大利好")
        self.assertEqual(events_store.notes_for("000657") if False else
                         events_store.list_events(limit=1)[0]["direction"], "中性")


class TestSeed(EventStoreCase):
    def test_seed_idempotent(self):
        created1, linked1 = events_store.seed_from_static()
        created2, linked2 = events_store.seed_from_static()
        self.assertGreater(created1, 0)
        self.assertEqual(created2, 0)        # 第二次全部命中
        self.assertEqual(events_store.stats()["events"], created1)

        # DB 里的事件数 = 静态字典去重后的 (date,title) 数
        pairs = {(n["date"], n["title"])
                 for notes in policy_notes.POLICY_NOTES.values() for n in notes}
        pairs |= {(n["date"], n["title"])
                  for notes in policy_notes.INDUSTRY_POLICY.values() for n in notes}
        self.assertEqual(created1, len(pairs))

    def test_seeded_notes_match_static(self):
        events_store.seed_from_static()
        db_notes = policy_notes.get_policy_notes("000657", "小金属")
        static_only = {n["title"] for n in policy_notes.POLICY_NOTES["000657"]}
        self.assertTrue(static_only.issubset({n["title"] for n in db_notes}))
        self.assertEqual(len(db_notes), len({(n["date"], n["title"]) for n in db_notes}))


class TestFallback(EventStoreCase):
    def test_falls_back_to_static_when_no_table(self):
        # 指向一个空库（还没建表）→ notes_for 返回 None → policy_notes 回退静态字典
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        notes = policy_notes.get_policy_notes("000657", "小金属")
        titles = {n["title"] for n in notes}
        self.assertIn("美国禁止废钨出口新规正式生效", titles)
        self.assertEqual(notes, sorted(notes, key=lambda x: x["date"], reverse=True))

    def test_empty_db_keeps_static(self):
        events_store.init_db()               # 建表但没数据
        notes = policy_notes.get_policy_notes("000657")
        self.assertTrue(notes)               # 空 DB 不能把页面变空
        self.assertIn("title", notes[0])


class TestExtract(unittest.TestCase):
    FAKE = {
        "choices": [{"message": {"content": (
            '{"date":"2026-11-07","title":"某部委收紧稀土配额",'
            '"summary":"供给端收缩，稀土价格中枢上移","direction":"利好","source":"财联社 2026-11-07"}'
        )}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40},
    }

    def test_extract_ok(self):
        calls = {}

        def fake_poster(url, headers, payload, timeout):
            calls["url"] = url
            calls["payload"] = payload
            return self.FAKE

        event, usage = events_store.extract_event(
            "财联社11月7日讯，某部委发布通知，收紧稀土开采配额……",
            api_key="sk-test", poster=fake_poster, today="2026-11-07")
        self.assertEqual(event["title"], "某部委收紧稀土配额")
        self.assertEqual(event["direction"], "利好")
        self.assertEqual(event["date"], "2026-11-07")
        self.assertEqual(usage["completion_tokens"], 40)
        self.assertIn("messages", calls["payload"])
        self.assertEqual(calls["payload"]["response_format"], {"type": "json_object"})

    def test_extract_rejects_empty_title(self):
        def fake_poster(url, headers, payload, timeout):
            return {"choices": [{"message": {"content": '{"title":"","summary":"x"}'}}]}
        with self.assertRaises(RuntimeError):
            events_store.extract_event("x", api_key="sk-test", poster=fake_poster)

    def test_extract_without_key(self):
        with mock.patch.object(events_store, "DEFAULT_DB", events_store.DEFAULT_DB):
            with self.assertRaises(RuntimeError):
                events_store.extract_event("x", api_key="", poster=lambda *a, **k: {})


if __name__ == "__main__":
    unittest.main()
