# -*- coding: utf-8 -*-
"""个股政策关联（P1）：关系类型 / 影响程度 / 机制 / 证据 / 审核状态 + /api/policy-events

要盯住的几件事：
  1. 只有事件显式挂了该股票代码，才算 direct（直接影响）
  2. 同行业命中只能算 industry（行业整体影响）——不能因为都在一个行业就标成直接利好
  3. 没有关联就返回空，不许按"同行业"或"名字像"去凑
  4. 库不可用（None）与库正常但没数据（[]）必须分开
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask                    # noqa: E402

import events_store                        # noqa: E402
import routes                              # noqa: E402


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="policy-test-")
        self.db = os.path.join(self.tmpdir, "events.db")
        self._env = mock.patch.dict(os.environ, {"STOCK_EVENTS_DB": self.db})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        for f in os.listdir(self.tmpdir):
            p = os.path.join(self.tmpdir, f)
            if os.path.isdir(p):
                os.rmdir(p)
            else:
                os.remove(p)
        os.rmdir(self.tmpdir)


class TestPolicyEventsFor(PolicyCase):
    def test_direct_link_is_marked_direct(self):
        events_store.add_event("2026-08-27", "钨出口禁令", "供给收缩", "利好",
                               "财联社", "https://example.com/a",
                               stocks=["000657"], industries=["小金属"])
        evs = events_store.notes_for("000657", "小金属")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["relation_type"], "direct")
        self.assertEqual(evs[0]["relation_label"], "直接影响")
        self.assertTrue(evs[0]["evidence"])
        self.assertIn("000657", evs[0]["evidence"])

    def test_same_industry_stock_gets_industry_not_direct(self):
        events_store.add_event("2026-08-27", "钨出口禁令", "供给收缩", "利好",
                               "财联社", "", industries=["小金属"])
        other = events_store.notes_for("002378", "小金属")
        self.assertEqual(len(other), 1)
        self.assertEqual(other[0]["relation_type"], "industry",
                         "同行业公司被标成了直接影响")
        self.assertIn("不代表个股直接受益", other[0]["evidence"])

    def test_no_relation_returns_empty_not_fabricated(self):
        events_store.add_event("2026-08-27", "钨出口禁令", "供给收缩", "利好",
                               "财联社", "", industries=["小金属"])
        self.assertEqual(events_store.notes_for("600519", "白酒"), [],
                         "给茅台编造了一条它没有的政策事件")

    def test_direct_sorted_before_industry(self):
        events_store.add_event("2026-09-01", "行业级新事", "", "中性", "", "",
                               industries=["小金属"])
        events_store.add_event("2026-03-01", "个股级旧事", "", "利好", "", "",
                               stocks=["000657"])
        evs = events_store.notes_for("000657", "小金属")
        self.assertEqual([e["relation_type"] for e in evs], ["direct", "industry"],
                         "直接影响没有排在行业事件前面")

    def test_unreviewed_stays_pending(self):
        events_store.add_event("2026-08-27", "没人审核过的事", "", "中性", "", "",
                               stocks=["000657"])
        self.assertEqual(events_store.notes_for("000657")[0]["review_status"], "pending")

    def test_impact_level_default_is_unknown(self):
        events_store.add_event("2026-08-27", "影响程度未知", "", "中性", "", "",
                               stocks=["000657"])
        ev = events_store.notes_for("000657")[0]
        self.assertEqual(ev["impact_level"], "待确认")
        self.assertTrue(ev["mechanism"] is not None)

    def test_missing_table_returns_none(self):
        """库打不开 → None（调用方据此回退静态字典），不能当成"没有政策"。"""
        import sqlite3
        with mock.patch.object(events_store, "connect", side_effect=sqlite3.OperationalError("no db")):
            self.assertIsNone(events_store.notes_for("000657"))

    def test_policy_events_for_matches_notes_for(self):
        events_store.add_event("2026-08-27", "钨出口禁令", "供给收缩", "利好",
                               "财联社", "", stocks=["000657"], industries=["小金属"])
        a = events_store.policy_events_for("000657", "中钨高新", "小金属")
        b = events_store.notes_for("000657", "小金属")
        self.assertEqual([e["event_id"] for e in a], [e["event_id"] for e in b])


class TestPolicyEventsApi(PolicyCase):
    def setUp(self):
        super().setUp()
        self.app = Flask(__name__)
        self.app.register_blueprint(routes.bp)
        self.client = self.app.test_client()

    def _get(self, qs):
        r = self.client.get("/api/policy-events?" + qs)
        return r.status_code, r.get_json()

    def test_bad_code_is_400(self):
        for bad in ("", "abc", "12", "6005190"):
            status, body = self._get("stock_code=" + bad)
            self.assertEqual(status, 400, bad)

    def test_real_events_payload_shape(self):
        events_store.add_event("2026-08-27", "钨出口禁令", "供给收缩", "利好",
                               "财联社", "https://example.com/a",
                               stocks=["000657"], industries=["小金属"])
        status, body = self._get("stock_code=000657&stock_name=%E4%B8%AD%E9%92%A8&industry=%E5%B0%8F%E9%87%91%E5%B1%9E")
        self.assertEqual(status, 200)
        d = body["data"]
        self.assertEqual(d["meta"]["data_status"], "real")
        self.assertEqual(d["meta"]["total"], 1)
        ev = d["events"][0]
        for key in ("event_id", "title", "occurred_at", "published_at", "relation_type",
                    "direction", "impact_level", "mechanism", "evidence",
                    "source_url", "review_status"):
            self.assertIn(key, ev)

    def test_empty_is_not_unavailable(self):
        status, body = self._get("stock_code=600519&industry=%E7%99%BD%E9%85%92")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["events"], [])
        self.assertEqual(body["data"]["meta"]["data_status"], "empty")

    def test_industry_default_lookup_is_tolerated(self):
        """不传 industry 时会去查行业；查不到也不许报错，更不能凭空关联。"""
        with mock.patch.object(routes, "get_industry_pe", side_effect=RuntimeError("net down")):
            status, body = self._get("stock_code=600519")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["meta"]["data_status"], "empty")

    # ---------------- 实时检索（外部接口全部 mock，不联网） ----------------

    def _live_item(self, title="某公司公告", origin="live"):
        return {
            "event_id": "live-1", "title": title, "summary": "",
            "occurred_at": None, "published_at": "2026-11-06",
            "collected_at": "2026-11-07 10:00:00", "date_known": False, "date": None,
            "relation_type": "direct", "relation_label": "直接影响（公司公告）",
            "direction": "待确认", "impact_level": "待确认", "mechanism": "",
            "evidence": "实时检索：东财公告接口（未审核）",
            "source": "东方财富·公司公告", "source_url": "https://example.com/ann",
            "review_status": "pending", "impact": "", "origin": origin,
        }

    def test_live_items_are_merged_and_marked_pending(self):
        import policy_live
        with mock.patch.object(policy_live, "search_with_status",
                               return_value=([self._live_item()], "success",
                                             {"announcements": "success"})):
            status, body = self._get("stock_code=600519&live=1")
        self.assertEqual(status, 200)
        d = body["data"]
        self.assertEqual(d["meta"]["live_total"], 1)
        ev = d["events"][0]
        self.assertEqual(ev["origin"], "live")
        self.assertEqual(ev["review_status"], "pending")
        self.assertEqual(ev["direction"], "待确认")

    def test_live_failure_does_not_break_db_events(self):
        import policy_live
        events_store.add_event("2026-08-27", "已入库的事件", "", "利好", "", "",
                               stocks=["000657"])
        with mock.patch.object(policy_live, "search_with_status",
                               side_effect=RuntimeError("net down")):
            status, body = self._get("stock_code=000657&industry=%E5%B0%8F%E9%87%91%E5%B1%9E&live=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]["events"]), 1)
        self.assertTrue(body["data"]["meta"]["live_status"].startswith("failed"))

    def test_default_does_not_hit_network(self):
        import policy_live
        with mock.patch.object(policy_live, "search", side_effect=AssertionError("不该联网")):
            status, _ = self._get("stock_code=600519&industry=%E7%99%BD%E9%85%92")
        self.assertEqual(status, 200)

    def test_live_does_not_duplicate_db_titles(self):
        import policy_live
        events_store.add_event("2026-08-27", "同一条公告", "", "利好", "", "",
                               stocks=["000657"])
        with mock.patch.object(policy_live, "search_with_status",
                               return_value=([self._live_item(title="同一条公告")],
                                             "success", {"announcements": "success"})):
            _, body = self._get("stock_code=000657&industry=%E5%B0%8F%E9%87%91%E5%B1%9E&live=1")
        titles = [e["title"] for e in body["data"]["events"]]
        self.assertEqual(titles.count("同一条公告"), 1)


if __name__ == "__main__":
    unittest.main()
