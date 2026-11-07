# -*- coding: utf-8 -*-
"""政策事件时间线口径（P1）：三个时间严格分离

被修的问题：
  1. （历史）事件页前端曾用 event_date 渲染日期 —— 该页已于 2026-11-07 下线，
     但下面这套时间口径仍被 events_store 与个股估值页的政策区块使用，必须继续锁住
  2. events_store.timeline 的"未分类"聚合还用 event_date>=?，和其它三处 COALESCE 口径不一致
     → 柱子加起来 ≠ 事件总数
  3. COALESCE 里带了 collected_at → 不知道哪天发生的事件被算成"今天发生"

新口径：
  occurred_at（发生） → published_at（发布） → 都没有 = 未知日期（独立计数，不进任何月份）
  collected_at 只作为页面元信息展示，不参与任何日期统计。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events_store  # noqa: E402


class TimelineDateCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="events-date-")
        self.db = os.path.join(self.tmpdir, "events.db")
        self._env = mock.patch.dict(os.environ, {"STOCK_EVENTS_DB": self.db})
        self._env.start()
        self.today_collected = "2026-11-07 10:00:00"

    def tearDown(self):
        self._env.stop()
        for f in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)

    def _add(self, title, occurred=None, published=None, industries=(), stocks=(), url=""):
        return events_store.add_event(
            None, title, "摘要", "中性", "测试来源", url,
            stocks=list(stocks), industries=list(industries),
            occurred_at=occurred, published_at=published,
            collected_at=self.today_collected)

    def test_unknown_date_event_is_not_counted_as_today(self):
        self._add("原文没写日期的事", industries=["小金属"])
        t = events_store.timeline(days=30)
        self.assertEqual(t["total"], 0, "未知日期的事件被算进了发生统计")
        self.assertEqual(t["undated"], 1)
        self.assertEqual(sum(sum(s["data"]) for s in t["series"]), 0)

    def test_published_at_decides_the_month(self):
        self._add("三月发布的事", published="2026-03-05", industries=["小金属"])
        t = events_store.timeline(days=3650)
        self.assertEqual(t["total"], 1)
        self.assertEqual(t["undated"], 0)
        ind = {s["name"]: s for s in t["series"]}["小金属"]
        self.assertEqual(ind["data"][t["months"].index("2026-03")], 1)

    def test_occurred_at_wins_over_published_at(self):
        self._add("一月发生六月发布", occurred="2026-01-09",
                  published="2026-06-30", industries=["小金属"])
        t = events_store.timeline(days=3650)
        ind = {s["name"]: s for s in t["series"]}["小金属"]
        self.assertEqual(ind["data"][t["months"].index("2026-01")], 1)
        self.assertEqual(ind["data"][t["months"].index("2026-06")], 0)

    def test_unclassified_uses_published_not_event_date(self):
        """原 bug：这里写的是 event_date>=?，而 event_date 对未知发生日期是空串。"""
        self._add("没有行业归属的事", published="2026-08-01")
        t = events_store.timeline(days=800)
        names = {s["name"]: s for s in t["series"]}
        self.assertIn("未分类", names, "未分类聚合丢了（还在用 event_date 过滤）")
        self.assertEqual(names["未分类"]["data"][t["months"].index("2026-08")], 1)

    def test_series_sum_matches_total(self):
        self._add("行业事件", occurred="2026-09-01", industries=["小金属"])
        self._add("无行业事件", published="2026-09-02")
        self._add("未知日期事件")
        t = events_store.timeline(days=800)
        self.assertEqual(t["total"], 2)
        self.assertEqual(sum(sum(s["data"]) for s in t["series"]), t["total"],
                         "柱子加起来和事件总数不一致")

    def test_list_events_can_include_undated(self):
        self._add("未知日期事件")
        without = events_store.list_events(since="2026-01-01")
        with_undated = events_store.list_events(since="2026-01-01", include_undated=True)
        self.assertEqual([e["title"] for e in without], [])
        self.assertEqual([e["title"] for e in with_undated], ["未知日期事件"])

    def test_month_filter_never_returns_undated(self):
        self._add("未知日期事件")
        self.assertEqual(events_store.list_events(month="2026-11"), [])
        self.assertEqual(events_store.list_events(month="2026-11", include_undated=True), [])

    def test_payload_carries_all_three_times_and_source_url(self):
        self._add("带链接的事", occurred="2026-10-01", published="2026-09-28",
                  industries=["小金属"], url="https://example.com/news/1")
        row = events_store.list_events(since="2026-01-01")[0]
        for key in ("occurred_at", "published_at", "collected_at", "source_url", "title"):
            self.assertIn(key, row)
        self.assertEqual(row["collected_at"], "2026-11-07 10:00")
        self.assertEqual(row["source_url"], "https://example.com/news/1")


if __name__ == "__main__":
    unittest.main()
