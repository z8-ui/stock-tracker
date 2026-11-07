# -*- coding: utf-8 -*-
"""快讯抓取/筛选测试（离线：把 _get 换成固定报文，不打网络）"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import event_feed  # noqa: E402

EM_BODY = 'var ajaxResult=' + json.dumps({
    "rc": 1,
    "LivesList": [
        {"title": "商务部：对钨相关物项实施出口管制",
         "digest": "【商务部：对钨相关物项实施出口管制】11月7日，商务部公告，对钨相关物项实施出口管制，即日生效。",
         "showtime": "2026-11-07 09:30:12",
         "url_w": "http://finance.eastmoney.com/a/1.html"},
        {"title": "某明星演唱会门票售罄",
         "digest": "【某明星演唱会门票售罄】开票 3 秒售罄。",
         "showtime": "2026-11-07 09:31:00",
         "url_w": "http://finance.eastmoney.com/a/2.html"},
        {"title": "", "digest": "空标题应被丢弃", "showtime": "2026-11-07 09:32:00"},
    ],
}, ensure_ascii=False)

SINA_BODY = json.dumps({"result": {"data": [
    {"title": "多项稀土配额指标下达", "intro": "配额同比减少 8%",
     "ctime": "1794012345", "url": "https://finance.sina.com.cn/a.shtml",
     "media_name": "财联社"},
]}}, ensure_ascii=False)


class FeedCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="feed-test-")
        self.db = os.path.join(self.tmpdir, "events.db")
        self._env = mock.patch.dict(os.environ, {"STOCK_EVENTS_DB": self.db})
        self._env.start()
        event_feed.init_seen()

    def tearDown(self):
        self._env.stop()
        for f in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)


class TestParse(FeedCase):
    def test_eastmoney_jsonp(self):
        with mock.patch.object(event_feed, "_get", return_value=EM_BODY.encode()):
            items = event_feed.fetch_eastmoney()
        self.assertEqual(len(items), 2)                      # 空标题被丢
        first = items[0]
        self.assertEqual(first["source"], "东财快讯")
        self.assertEqual(first["published_at"], "2026-11-07 09:30")
        self.assertTrue(first["summary"].startswith("11月7日"))   # 【标题】前缀已剥掉
        self.assertNotIn("【商务部", first["summary"])

    def test_sina(self):
        with mock.patch.object(event_feed, "_get", return_value=SINA_BODY.encode()):
            items = event_feed.fetch_sina()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "多项稀土配额指标下达")
        self.assertTrue(items[0]["published_at"].startswith("20"))

    def test_source_failure_isolated(self):
        def boom(url, timeout=12):
            raise OSError("blocked")

        with mock.patch.object(event_feed, "_get", side_effect=boom):
            items, errors, ok = event_feed.fetch_headlines()
        self.assertEqual(items, [])
        self.assertEqual(len(errors), 2)                     # 两个源各自报错，不抛异常
        self.assertEqual(ok, 0)                              # 全失败 → 成功源数 0


class TestScreen(FeedCase):
    def test_keyword_hit_and_miss(self):
        items = [
            {"source": "东财快讯", "source_key": "eastmoney", "title": "对钨相关物项实施出口管制",
             "summary": "即日生效", "url": "", "published_at": ""},
            {"source": "东财快讯", "source_key": "eastmoney", "title": "某明星演唱会门票售罄",
             "summary": "3 秒售罄", "url": "", "published_at": ""},
        ]
        kept = event_feed.screen(items, keywords=["出口管制"])
        self.assertEqual(len(kept), 1)
        self.assertIn("出口管制", kept[0]["hits"])
        self.assertEqual(len(kept[0]["fp"]), 16)

    def test_custom_keyword(self):
        items = [{"source": "x", "source_key": "sina", "title": "钨精矿价格上调",
                  "summary": "", "url": "", "published_at": ""}]
        kept = event_feed.screen(items, keywords=["钨精矿"])
        self.assertEqual(len(kept), 1)              # 自定义词是"加"，不是"替换"默认词
        self.assertIn("钨精矿", kept[0]["hits"])
        noise = [{"source": "x", "source_key": "sina", "title": "某地发布暴雨橙色预警",
                  "summary": "未来 6 小时降雨量将达 50 毫米", "url": "", "published_at": ""}]
        self.assertEqual(event_feed.screen(noise, keywords=["钨精矿"]), [])


class TestSeen(FeedCase):
    def test_mark_and_filter(self):
        items = [{"source": "东财快讯", "source_key": "eastmoney", "title": "A",
                  "summary": "", "url": "", "published_at": ""},
                 {"source": "东财快讯", "source_key": "eastmoney", "title": "B",
                  "summary": "", "url": "", "published_at": ""}]
        kept = event_feed.screen(items, keywords=["A", "B"])
        self.assertEqual(len(kept), 2)
        event_feed.mark(kept[:1])                 # 只展示 → pending，不算已处理
        self.assertNotIn(kept[0]["fp"], event_feed.seen_fps())
        self.assertIn(kept[0]["fp"], event_feed.pending_fp())
        event_feed.mark(kept[1:], review="rejected")   # 人工驳回 → 不再出现
        seen = event_feed.seen_fps()
        self.assertIn(kept[1]["fp"], seen)
        self.assertNotIn(kept[1]["fp"], event_feed.pending_fp())

    def test_mark_is_idempotent(self):
        item = {"source": "东财快讯", "source_key": "eastmoney", "title": "C",
                "summary": "", "url": "", "published_at": ""}
        kept = event_feed.screen([item], keywords=["C"])
        event_feed.mark(kept)                                  # 展示
        event_feed.mark(kept, process="ingested", review="approved")
        rows = event_feed.recent_seen(days=1)
        self.assertEqual(len(rows), 1)                        # 不重复插入
        self.assertEqual(rows[0]["review_status"], "approved")
        self.assertEqual(rows[0]["process_status"], "ingested")

    def test_to_event_text(self):
        text = event_feed.to_event_text(
            {"title": "T", "summary": "S", "source": "东财快讯", "published_at": "2026-11-07 09:30"})
        self.assertIn("T", text)
        self.assertIn("S", text)
        self.assertIn("东财快讯", text)


class TestWeightAndDedupe(FeedCase):
    def _item(self, title, summary="", when="2026-11-07 09:00"):
        return {"source": "东财快讯", "source_key": "eastmoney", "title": title,
                "summary": summary, "url": "", "published_at": when}

    def test_weight_policy_beats_market(self):
        cands = event_feed.screen([
            self._item("商务部对钨相关物项实施出口管制"),
            self._item("某公司发布业绩预告"),
        ], keywords=[])
        by_title = {c["title"]: c for c in cands}
        self.assertEqual(by_title["商务部对钨相关物项实施出口管制"]["weight"], 2)
        self.assertEqual(by_title["某公司发布业绩预告"]["weight"], 1)

    def test_watchlist_code_marks_strong(self):
        # 不依赖 config_local（发布包里没有它），把自选池固定成 000657
        with mock.patch.object(event_feed, "_watch_codes", return_value={"000657"}):
            cands = event_feed.screen([self._item("000657 发布公告")], keywords=[])
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["codes"], ["000657"])
        self.assertEqual(cands[0]["weight"], 2)      # 自选股代码命中 → 强相关

    def test_watchlist_code_without_config(self):
        with mock.patch.object(event_feed, "_watch_codes", return_value=set()):
            cands = event_feed.screen([self._item("000657 发布公告")], keywords=[])
        self.assertEqual(cands, [])

    def test_dedupe_near_duplicates(self):
        items = [
            self._item("000520，被立案"),
            self._item("000520，被证监会立案"),
            self._item("工信部下达稀土开采配额指标"),
        ]
        cands = event_feed.screen(items, keywords=["立案", "配额"])
        kept, merged = event_feed.dedupe(cands)
        self.assertEqual(merged, 1)
        self.assertEqual(len(kept), 2)
        dup = [k for k in kept if k["dup_count"] > 1]
        self.assertEqual(len(dup), 1)
        self.assertIn("证监会", dup[0]["title"])     # 合并后保留信息更全的那条

    def test_dedupe_keeps_distinct(self):
        items = [self._item("商务部对钨相关物项实施出口管制"),
                 self._item("发改委下达稀土配额总量控制指标")]
        cands = event_feed.screen(items, keywords=[])
        self.assertEqual(len(cands), 2)
        kept, merged = event_feed.dedupe(cands)
        self.assertEqual((len(kept), merged), (2, 0))


if __name__ == "__main__":
    unittest.main()
