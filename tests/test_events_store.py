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

    # ---- 场景 C：已有正式事件不被 seed 破坏 ----

    def test_seed_preserves_existing_user_event(self):
        """C-1：用户先录的事件，seed 后仍在，且内容一个字都没被改。"""
        eid, _, _ = events_store.add_event(
            "2026-10-01", "用户自建：某公司扩产公告", "人工写的影响说明",
            "利好", "公司公告", stocks=["000001"], review_status="pending")
        before = dict(events_store.list_events(limit=50)[0])

        events_store.seed_from_static()

        rows = {r["id"]: r for r in events_store.list_events(limit=50)}
        self.assertIn(eid, rows)
        after = rows[eid]
        for field in ("event_date", "title", "summary", "direction", "source",
                      "fingerprint", "review_status", "status", "evidence"):
            self.assertEqual(after[field], before[field], f"{field} 被 seed 改动了")
        self.assertEqual(len(rows), events_store.stats()["events"])  # 只有种子条目多出来
        self.assertIn("用户自建：某公司扩产公告", {r["title"] for r in rows.values()})

    def test_seed_does_not_overwrite_same_fingerprint_event(self):
        """C-2：库里已有「同日期同标题」事件时，seed 不能拿静态字典覆盖它。

        这是最容易踩的坑：指纹相同 → add_event 走"已存在"分支。
        静态池里的 impact/direction 与库里人工改过的版本不同，必须以库里为准。
        """
        static_code, static_note = next(iter(policy_notes.POLICY_NOTES.items()))
        note = static_note[0]
        eid, created, _ = events_store.add_event(
            note["date"], note["title"], "人工改写过的说明（不是静态字典那句）",
            "利空", "人工核实", stocks=[static_code], review_status="approved")
        self.assertTrue(created)

        events_store.seed_from_static()
        events_store.seed_from_static()          # 连跑两次，确认也不会被覆盖

        row = [r for r in events_store.list_events(limit=200) if r["id"] == eid][0]
        self.assertEqual(row["summary"], "人工改写过的说明（不是静态字典那句）")
        self.assertEqual(row["direction"], "利空")
        self.assertEqual(row["source"], "人工核实")
        self.assertEqual(row["review_status"], "approved")

    # ---- 场景 D：人工审核状态不被 seed 覆盖 ----

    def test_seed_keeps_approved_review_status(self):
        """D：标成 approved 的事件，seed 后仍是 approved，审核记录不丢。"""
        eid, _, _ = events_store.add_event(
            "2026-09-15", "待人工审核的事件", "说明", stocks=["600519"],
            review_status="pending")
        conn = events_store.connect()
        conn.execute("UPDATE events SET review_status='approved', "
                     "impact_level='直接受影响', mechanism='成本上升' WHERE id=?", (eid,))
        conn.commit()
        conn.close()

        events_store.seed_from_static()
        events_store.seed_from_static()

        conn = events_store.connect()
        row = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["review_status"], "approved")
        self.assertEqual(row["impact_level"], "直接受影响")
        self.assertEqual(row["mechanism"], "成本上升")
        # seed 新插入的静态条目本身带 approved（静态池是人工维护的），
        # 但不能因此把"人工改过的 pending 事件"也顺手改成 approved
        others = events_store.list_events(limit=200)
        self.assertTrue(all(r["review_status"] in ("approved", "pending") for r in others))

    def test_seed_does_not_flip_pending_to_approved(self):
        """D-2：人工明确标成 pending 的既有事件，seed 不得把它升成 approved。"""
        note = next(iter(policy_notes.POLICY_NOTES.values()))[0]
        eid, _, _ = events_store.add_event(
            note["date"], note["title"], "人工待审", "中性", "", review_status="pending")
        events_store.seed_from_static()
        conn = events_store.connect()
        status = conn.execute("SELECT review_status FROM events WHERE id=?", (eid,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "pending")

    # ---- 场景 E：事务原子性（真实数据库状态校验） ----

    def test_seed_rolls_back_entirely_on_midway_failure(self):
        """E：中途写入失败 → 已写的事件全部回滚，库里没有半截初始化数据。"""
        real_add = events_store.add_event
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            result = real_add(*args, **kwargs)      # 先真的写进去
            if calls["n"] >= 3:
                raise sqlite3.OperationalError("disk I/O error（模拟中途失败）")
            return result

        with mock.patch.object(events_store, "add_event", side_effect=flaky):
            with self.assertRaises(sqlite3.OperationalError):
                events_store.seed_from_static()

        self.assertGreaterEqual(calls["n"], 3)      # 确认确实写到第 3 条才炸

        # 用全新连接读文件本身，不看异常、只看落盘结果
        conn = sqlite3.connect(self.db)
        try:
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            links = conn.execute("SELECT COUNT(*) FROM event_links").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(events, 0, f"回滚失败：残留 {events} 条事件")
        self.assertEqual(links, 0, f"回滚失败：残留 {links} 条关联")

        # 半截数据清理后，正常再跑一次必须能完整初始化
        created, linked = events_store.seed_from_static()
        pairs = {(n["date"], n["title"])
                 for notes in policy_notes.POLICY_NOTES.values() for n in notes}
        pairs |= {(n["date"], n["title"])
                  for notes in policy_notes.INDUSTRY_POLICY.values() for n in notes}
        self.assertEqual(created, len(pairs))
        self.assertGreater(linked, 0)
        self.assertEqual(events_store.stats()["events"], len(pairs))

        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                             len(pairs))
        finally:
            conn.close()

    def test_seed_rollback_leaves_no_partial_when_failure_on_first_row(self):
        """E-2：第一条就失败时也不能建出"空壳半成品"——不残留任何事件。"""
        with mock.patch.object(events_store, "add_event",
                               side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(sqlite3.OperationalError):
                events_store.seed_from_static()
        self.assertEqual(events_store.stats()["events"], 0)

    def test_seed_commits_on_success(self):
        """事务修复不能把成功路径也弄成"只在当前连接可见"。"""
        events_store.seed_from_static()
        conn = sqlite3.connect(self.db)      # 另一个连接读文件
        try:
            n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, events_store.stats()["events"])
        self.assertGreater(n, 0)

    # ---- 事务嵌套语义（F/G） ----

    def test_transaction_commits_when_it_is_outermost(self):
        """最外层 SAVEPOINT 的 RELEASE 必须等价于提交。"""
        conn = events_store.connect()
        conn.execute("CREATE TABLE IF NOT EXISTS marker (x TEXT)")
        with events_store.transaction(conn):
            conn.execute("INSERT INTO marker (x) VALUES ('a')")
        conn.close()
        other = sqlite3.connect(self.db)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 1)
        finally:
            other.close()

    def test_nested_transaction_does_not_commit_outer(self):
        """嵌套 SAVEPOINT 成功只释放本层，外层仍未提交。"""
        conn = events_store.connect()
        conn.execute("CREATE TABLE IF NOT EXISTS marker (x TEXT)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO marker (x) VALUES ('outer')")
        with events_store.transaction(conn):
            conn.execute("INSERT INTO marker (x) VALUES ('inner')")
        self.assertTrue(conn.in_transaction)          # 外层还在
        other = sqlite3.connect(self.db)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 0)
        finally:
            other.close()
        conn.commit()
        conn.close()
        other = sqlite3.connect(self.db)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 2)
        finally:
            other.close()

    # ---- 场景 F：外层事务中的 seed 成功 ----

    def test_seed_inside_outer_transaction_does_not_commit(self):
        """F：seed 在外层事务里成功，第二个连接看不到任何未提交数据。"""
        events_store.init_db()
        conn = events_store.connect()
        conn.execute("BEGIN IMMEDIATE")
        events_store.add_event("2026-11-01", "外层标记事件", "外层写入", stocks=["000001"],
                               conn=conn, commit=False)
        self.assertTrue(conn.in_transaction)

        created, linked = events_store.seed_from_static(conn=conn)
        self.assertGreater(created, 0)
        self.assertGreater(linked, 0)
        self.assertTrue(conn.in_transaction, "seed 把调用方事务提交掉了")

        other = sqlite3.connect(self.db)          # 第二个连接：什么都看不到
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            self.assertEqual(other.execute("SELECT COUNT(*) FROM event_links").fetchone()[0], 0)
        finally:
            other.close()

        conn.commit()                             # 调用方自己提交后才可见
        conn.close()
        other = sqlite3.connect(self.db)
        try:
            total = other.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            titles = {r[0] for r in other.execute("SELECT title FROM events")}
        finally:
            other.close()
        self.assertEqual(total, created + 1)
        self.assertIn("外层标记事件", titles)

    # ---- 场景 G：外层事务中的 seed 失败 ----

    def test_seed_failure_inside_outer_transaction_keeps_outer_work(self):
        """G：seed 中途失败只回滚自己那一层，外层标记记录还在、外层仍可提交。"""
        events_store.init_db()
        conn = events_store.connect()
        conn.execute("BEGIN IMMEDIATE")
        events_store.add_event("2026-11-01", "外层标记事件", "外层写入", stocks=["000001"],
                               conn=conn, commit=False)

        real_add = events_store.add_event
        state = {"n": 0}

        def flaky(*args, **kwargs):
            state["n"] += 1
            result = real_add(*args, **kwargs)
            if state["n"] >= 3:
                raise sqlite3.OperationalError("boom（模拟 seed 中途失败）")
            return result

        with mock.patch.object(events_store, "add_event", side_effect=flaky):
            with self.assertRaises(sqlite3.OperationalError):
                events_store.seed_from_static(conn=conn)

        self.assertTrue(conn.in_transaction, "种子失败错误地把外层事务也回滚了")
        titles = [r[0] for r in conn.execute("SELECT title FROM events")]
        self.assertEqual(titles, ["外层标记事件"])          # seed 的写入全撤了
        # 只剩外层那条的关联（1 条，指向 000001）；seed 的 15 条关联必须全部撤销
        links = conn.execute("SELECT target_key FROM event_links").fetchall()
        self.assertEqual([r[0] for r in links], ["000001"])

        conn.commit()                                       # 外层仍能正常提交
        conn.close()
        other = sqlite3.connect(self.db)
        try:
            self.assertEqual([r[0] for r in other.execute("SELECT title FROM events")],
                             ["外层标记事件"])
        finally:
            other.close()

    def test_seed_failure_inside_outer_transaction_can_still_rollback(self):
        """G-2：失败后外层也可以选择整体回滚，同样不能卡住。"""
        events_store.init_db()
        conn = events_store.connect()
        conn.execute("BEGIN IMMEDIATE")
        events_store.add_event("2026-11-01", "外层标记事件", conn=conn,
                               stocks=["000001"], commit=False)
        with mock.patch.object(events_store, "add_event",
                               side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(sqlite3.OperationalError):
                events_store.seed_from_static(conn=conn)
        conn.rollback()                                     # 外层主动回滚
        conn.close()
        other = sqlite3.connect(self.db)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        finally:
            other.close()

    # ---- 场景 H：禁止 own=True + commit=False ----

    def test_rejects_own_connection_without_commit(self):
        """H：add_event(conn=None, commit=False) 必须在建连接之前就拒绝。"""
        self.assertFalse(os.path.exists(self.db), "前置条件：库里还不该有文件")
        with self.assertRaises(ValueError) as ctx:
            events_store.add_event("2026-11-01", "危险组合", commit=False)
        self.assertIn("commit=False", str(ctx.exception))
        self.assertIn("conn", str(ctx.exception))
        self.assertFalse(os.path.exists(self.db), "拒绝应当发生在 connect() 之前")

    # ---- 场景 I：Schema 迁移边界 ----

    OLD_SCHEMA = """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT '中性',
            source TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        );
        CREATE TABLE event_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            target_type TEXT NOT NULL,
            target_key TEXT NOT NULL,
            UNIQUE(event_id, target_type, target_key)
        );
        CREATE TABLE marker (x TEXT);
    """

    def _make_old_schema(self):
        raw = sqlite3.connect(self.db)
        try:
            raw.executescript(self.OLD_SCHEMA)
            raw.commit()
        finally:
            raw.close()
        self.assertFalse(events_store._schema_ready(events_store.connect()))

    def test_schema_migration_refused_inside_outer_transaction(self):
        """I：结构不完整 + 连接已在事务中 → 明确拒绝，且不得隐式提交外层事务。"""
        self._make_old_schema()
        conn = events_store.connect()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO marker (x) VALUES ('outer')")

        with self.assertRaises(RuntimeError) as ctx:
            events_store.seed_from_static(conn=conn)
        self.assertIn("事务", str(ctx.exception))

        self.assertTrue(conn.in_transaction, "executescript 把调用方事务隐式提交了")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

        other = sqlite3.connect(self.db)          # 未提交 → 外部看不到
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 0)
        finally:
            other.close()

        conn.commit()                             # 外层照常提交
        conn.close()
        other = sqlite3.connect(self.db)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 1)
        finally:
            other.close()

    def test_old_schema_migrates_outside_transaction(self):
        """I-2：旧库在事务外调用时仍能正常迁移 + seed（不能因为加了守卫而迁不动）。"""
        self._make_old_schema()
        created, linked = events_store.seed_from_static()
        self.assertEqual(created, 4)
        self.assertGreater(linked, 0)
        conn = events_store.connect()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
            self.assertIn("review_status", cols)
            self.assertIn("evidence", cols)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 4)
        finally:
            conn.close()


class TestSchemaContract(EventStoreCase):
    """schema 契约：结构判定必须覆盖 event_links 与约束/索引；迁移不得动调用方事务。"""

    def _raw(self, script):
        raw = sqlite3.connect(self.db)
        try:
            raw.executescript(script)
            raw.commit()
        finally:
            raw.close()

    def _ready(self):
        conn = events_store.connect()
        try:
            return events_store._schema_ready(conn)
        finally:
            conn.close()

    def test_fresh_db_is_ready(self):
        events_store.init_db()
        self.assertTrue(self._ready())

    def test_events_complete_but_event_links_missing(self):
        """events 齐全、event_links 不存在 → 不能判就绪；seed 要能把它补建出来。"""
        events_store.init_db()
        self._raw("DROP TABLE event_links;")
        self.assertFalse(self._ready())

        created, linked = events_store.seed_from_static()
        self.assertEqual(created, 4)
        self.assertGreater(linked, 0)
        conn = events_store.connect()
        try:
            self.assertGreater(
                conn.execute("SELECT COUNT(*) FROM event_links").fetchone()[0], 0)
        finally:
            conn.close()
        self.assertTrue(self._ready())

    def test_event_links_missing_relation_type(self):
        """event_links 缺 relation_type → 判不就绪；迁移补列并回填 direct/industry。"""
        events_store.init_db()
        self._raw("""
            DROP INDEX IF EXISTS idx_links_target;
            DROP TABLE event_links;
            CREATE TABLE event_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                target_type TEXT NOT NULL,
                target_key TEXT NOT NULL,
                UNIQUE(event_id, target_type, target_key)
            );
        """)
        self.assertFalse(self._ready())

        created, _ = events_store.seed_from_static()
        self.assertEqual(created, 4)
        conn = events_store.connect()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(event_links)").fetchall()}
            self.assertIn("relation_type", cols)
            rows = conn.execute(
                "SELECT target_type, relation_type FROM event_links").fetchall()
            self.assertTrue(rows)
            for target_type, rel in rows:
                self.assertEqual(rel, "direct" if target_type == "stock" else "industry")
        finally:
            conn.close()
        self.assertTrue(self._ready())

    def test_events_missing_published_and_collected(self):
        """events 缺 published_at / collected_at → 判不就绪；迁移补列后 seed 正常。"""
        self._raw("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_date TEXT NOT NULL, title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT '中性',
                source TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'active',
                occurred_at TEXT,
                impact_level TEXT NOT NULL DEFAULT '待确认',
                mechanism TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
                review_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
            );
            CREATE INDEX idx_events_date ON events(event_date DESC);
            CREATE TABLE event_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                target_type TEXT NOT NULL, target_key TEXT NOT NULL,
                relation_type TEXT NOT NULL DEFAULT '',
                UNIQUE(event_id, target_type, target_key)
            );
            CREATE INDEX idx_links_target ON event_links(target_type, target_key);
        """)
        self.assertFalse(self._ready())

        created, _ = events_store.seed_from_static()
        self.assertEqual(created, 4)
        conn = events_store.connect()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
            self.assertIn("published_at", cols)
            self.assertIn("collected_at", cols)
        finally:
            conn.close()
        self.assertTrue(self._ready())

    def test_missing_index_is_not_ready(self):
        """SCHEMA 里的索引缺失 → 判不就绪；seed 会重建（CREATE INDEX IF NOT EXISTS）。"""
        events_store.init_db()
        self._raw("DROP INDEX idx_events_date;")
        self.assertFalse(self._ready())
        events_store.seed_from_static()
        conn = events_store.connect()
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        finally:
            conn.close()
        self.assertIn("idx_events_date", names)
        self.assertTrue(self._ready())

    def test_missing_unique_constraint_is_reported_not_hidden(self):
        """events.fingerprint 没有 UNIQUE → init_db 修不了，必须明确报错而不是假装成功。"""
        self._raw("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_date TEXT NOT NULL, title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT '中性',
                source TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                occurred_at TEXT, published_at TEXT, collected_at TEXT,
                impact_level TEXT NOT NULL DEFAULT '待确认',
                mechanism TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
                review_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
            );
            CREATE INDEX idx_events_date ON events(event_date DESC);
            CREATE TABLE event_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL, target_type TEXT NOT NULL,
                target_key TEXT NOT NULL, relation_type TEXT NOT NULL DEFAULT '',
                UNIQUE(event_id, target_type, target_key)
            );
            CREATE INDEX idx_links_target ON event_links(target_type, target_key);
        """)
        self.assertFalse(self._ready())
        with self.assertRaises(RuntimeError) as ctx:
            events_store.seed_from_static()
        self.assertIn("无法自动迁移", str(ctx.exception))

    def test_index_signatures_match_schema(self):
        """正常库：两个索引的表、列、顺序、DESC 全部对得上。"""
        events_store.init_db()
        conn = events_store.connect()
        try:
            self.assertEqual(events_store._index_signature(conn, "idx_events_date"),
                             ("events", (("event_date", True),), False))
            self.assertEqual(events_store._index_signature(conn, "idx_links_target"),
                             ("event_links", (("target_type", False),
                                              ("target_key", False)), False))
        finally:
            conn.close()
        self.assertTrue(self._ready())

    def test_index_right_name_wrong_column(self):
        """idx_events_date 名字对，但建在 events.title 上 → 必须判不就绪。"""
        events_store.init_db()
        self._raw("""
            DROP INDEX idx_events_date;
            CREATE INDEX idx_events_date ON events(title DESC);
        """)
        self.assertFalse(self._ready())
        with self.assertRaises(RuntimeError) as ctx:
            events_store.seed_from_static()
        self.assertIn("无法自动迁移", str(ctx.exception))

    def test_index_right_name_wrong_table(self):
        """idx_events_date 名字对，但建在 event_links 表上 → 必须判不就绪。"""
        events_store.init_db()
        self._raw("""
            DROP INDEX idx_events_date;
            CREATE INDEX idx_events_date ON event_links(target_type);
        """)
        self.assertFalse(self._ready())

    def test_links_index_column_order_wrong(self):
        """idx_links_target 列顺序反了（target_key, target_type）→ 必须判不就绪。"""
        events_store.init_db()
        self._raw("""
            DROP INDEX idx_links_target;
            CREATE INDEX idx_links_target ON event_links(target_key, target_type);
        """)
        self.assertFalse(self._ready())

    def test_events_index_without_desc(self):
        """idx_events_date 少了 DESC（默认 ASC）→ 必须判不就绪。"""
        events_store.init_db()
        self._raw("""
            DROP INDEX idx_events_date;
            CREATE INDEX idx_events_date ON events(event_date);
        """)
        self.assertFalse(self._ready())
        sig = None
        conn = events_store.connect()
        try:
            sig = events_store._index_signature(conn, "idx_events_date")
        finally:
            conn.close()
        self.assertEqual(sig, ("events", (("event_date", False),), False))   # 确认真的读成 ASC

    def test_partial_unique_index_is_not_a_real_constraint(self):
        """事件表只有 partial unique index（带 WHERE）→ 不能当成完整唯一约束。"""
        self._raw("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_date TEXT NOT NULL, title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT '中性',
                source TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                occurred_at TEXT, published_at TEXT, collected_at TEXT,
                impact_level TEXT NOT NULL DEFAULT '待确认',
                mechanism TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
                review_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
            );
            CREATE INDEX idx_events_date ON events(event_date DESC);
            CREATE UNIQUE INDEX idx_dead_fp ON events(fingerprint) WHERE fingerprint <> '';
            CREATE TABLE event_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL, target_type TEXT NOT NULL,
                target_key TEXT NOT NULL, relation_type TEXT NOT NULL DEFAULT '',
                UNIQUE(event_id, target_type, target_key)
            );
            CREATE INDEX idx_links_target ON event_links(target_type, target_key);
        """)
        conn = events_store.connect()
        try:
            self.assertNotIn(("fingerprint",), events_store._unique_column_sets(conn, "events"))
            self.assertEqual(events_store._unique_column_sets(conn, "events"), set())
        finally:
            conn.close()
        self.assertFalse(self._ready())
        with self.assertRaises(RuntimeError) as ctx:
            events_store.seed_from_static()
        self.assertIn("无法自动迁移", str(ctx.exception))

    # ---- init_db 的事务契约 ----

    def test_init_db_refuses_inside_outer_transaction(self):
        events_store.init_db()                     # 先在事务外建好
        conn = events_store.connect()
        conn.execute("CREATE TABLE IF NOT EXISTS marker (x TEXT)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO marker (x) VALUES ('outer')")

        with self.assertRaises(RuntimeError) as ctx:
            events_store.init_db(conn)
        self.assertIn("事务", str(ctx.exception))
        self.assertTrue(conn.in_transaction, "init_db 把调用方事务提交掉了")

        other = sqlite3.connect(self.db)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 0)
        finally:
            other.close()

        conn.commit()
        conn.close()
        other = sqlite3.connect(self.db)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 1)
        finally:
            other.close()

    def test_init_db_refusal_keeps_outer_rollback_working(self):
        events_store.init_db()
        conn = events_store.connect()
        conn.execute("CREATE TABLE IF NOT EXISTS marker (x TEXT)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO marker (x) VALUES ('outer')")
        with self.assertRaises(RuntimeError):
            events_store.init_db(conn)
        conn.rollback()                            # 外层主动回滚仍然有效
        conn.close()
        other = sqlite3.connect(self.db)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 0)
        finally:
            other.close()

    def test_init_db_own_connection_still_works(self):
        """自己连自己建（CLI / 页面首次启动路径）不受事务契约影响。"""
        events_store.init_db()
        events_store.init_db()                     # 幂等重跑
        self.assertTrue(self._ready())


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


class TestMonthFilter(EventStoreCase):
    def test_timeline_month_filters_events_only(self):
        events_store.add_event("2026-08-27", "八月的事", stocks=["000657"], industries=["小金属"])
        events_store.add_event("2026-06-10", "六月的事", stocks=["000657"], industries=["小金属"])
        events_store.add_event("2025-02-04", "去年的事", stocks=["000657"], industries=["小金属"])

        t = events_store.timeline(days=800, month="2026-08")
        self.assertEqual(t["month"], "2026-08")
        self.assertEqual(len(t["events"]), 1)
        self.assertEqual(t["events"][0]["title"], "八月的事")
        self.assertEqual(t["month_total"], 1)
        self.assertEqual(t["total"], 3)          # 全局口径不受月份筛选影响
        self.assertEqual(sum(t["series"][0]["data"]), 3)

    def test_timeline_month_with_industry(self):
        events_store.add_event("2026-08-27", "钨的事", stocks=["000657"], industries=["小金属"])
        events_store.add_event("2026-08-28", "酒的事", stocks=["600519"], industries=["白酒"])
        t = events_store.timeline(days=800, industry="小金属", month="2026-08")
        self.assertEqual([e["title"] for e in t["events"]], ["钨的事"])

    def test_list_events_month(self):
        events_store.add_event("2026-08-27", "A", stocks=["000657"])
        events_store.add_event("2026-09-01", "B", stocks=["000657"])
        rows = events_store.list_events(month="2026-08", code="000657")
        self.assertEqual([r["title"] for r in rows], ["A"])


if __name__ == "__main__":
    unittest.main()
