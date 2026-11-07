"""V2.0.2 质量完善回归测试

覆盖本轮改动中"光看代码看不出来、必须跑"的四件事：
  1. 发布包扫描器：JSON 转义字段名（api\\u005fkey）不得绕过结构化扫描；
  2. 政策检索状态：success / empty / partial / failed 不能混为一谈；
  3. 关键词：来自真实经营范围、可回溯，且不能把整句当词；
  4. 数据健康：不联网、状态按真实时间给，单源失败不牵连全局。

所有外部请求一律 mock，不消耗真实额度、不依赖网络。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "scripts"))


class TestScannerJsonEscapes(unittest.TestCase):
    """转义字段名 / 转义字符串 / 嵌套 / 数组 / 跨块 —— 大小文件同口径。"""

    def setUp(self):
        import package_release as pr
        self.pr = pr
        self.tmp = tempfile.mkdtemp()

    def _scan(self, content, name="t.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return self.pr.scan_file(path, name)

    def test_escaped_key_detected(self):
        hits = self._scan('{"api\\u005fkey": "zzzzzzzzzzzzzzzz"}')
        self.assertEqual(len(hits), 1)
        self.assertIn("api_key", hits[0][1])

    def test_escaped_nested_key_detected(self):
        hits = self._scan('{"outer": {"to\\u006ben": "1111111111111111"}}')
        self.assertEqual(len(hits), 1)
        self.assertIn("token", hits[0][1])

    def test_escaped_and_plain_same_verdict(self):
        a = self._scan('{"api\\u005fkey": "zzzzzzzzzzzzzzzz"}', "a.json")
        b = self._scan('{"api_key": "zzzzzzzzzzzzzzzz"}', "b.json")
        self.assertEqual(len(a), len(b), "转义写法与明文写法必须同等判定")

    def test_mention_inside_value_is_not_a_hit(self):
        hits = self._scan('{"note": "api\\\\u005fkey 只是文字说明"}')
        self.assertEqual(hits, [], "值里提到字段名不能误报")

    def test_array_credential_detected(self):
        hits = self._scan('{"tokens": ["aaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]}')
        self.assertEqual(len(hits), 2, "敏感键下的数组每个元素都要独立计数")

    def test_ordinary_array_not_flagged(self):
        hits = self._scan('{"arr": ["aaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]}')
        self.assertEqual(hits, [], "普通数组不能被当成凭据")

    def test_across_chunk_boundary(self):
        """同一份 JSON 按 97 字节切块喂进去，命中的键名集合必须与整读一致。"""
        payload = ('{"api\\u005fkey": "zzzzzzzzzzzzzzzz", "tokens": ['
                   + ",".join(['"value%04dxxxx"' % i for i in range(300)]) + "]}").encode()
        path = os.path.join(self.tmp, "whole.json")
        with open(path, "wb") as f:
            f.write(payload)
        whole = sorted(h[1] for h in self.pr.scan_file(path, "whole.json"))

        scanner = self.pr._CredentialJsonScanner("whole.json")
        for i in range(0, len(payload), 97):
            scanner.feed(payload[i:i + 97])       # feed() 返回累计结果，取最后一次即可
        streamed = sorted(h[1] for h in scanner.feed(b"", final=True))
        # 标签格式由 scan_file 补数组下标（tokens[0]），流式调用不补 —— 比对"命中数 + 键名"
        def keyof(label):
            return label.split(": ", 1)[-1].split("=", 1)[0].split("[", 1)[0]
        self.assertEqual(len(whole), len(streamed), "跨块不能漏检")
        self.assertEqual(sorted({keyof(x) for x in whole}),
                         sorted({keyof(x) for x in streamed}))
        self.assertEqual(len(whole), 301, "1 个键 + 300 个数组元素都要报")


class TestPolicyStatus(unittest.TestCase):
    """2.4：四种状态分开，失败绝不能显示成"没有政策"。"""

    def test_all_failed_is_failed(self):
        import policy_live
        self.assertEqual(policy_live._merge_status("failed: HTTP 500",
                                                   "failed: Timeout"), "failed")

    def test_one_failed_is_partial(self):
        import policy_live
        self.assertEqual(policy_live._merge_status("success", "failed: Timeout"), "partial")

    def test_source_partial_is_partial(self):
        import policy_live
        self.assertEqual(policy_live._merge_status("empty", "partial"), "partial")

    def test_all_ok_with_items_is_success(self):
        import policy_live
        self.assertEqual(policy_live._merge_status("success", "success"), "success")

    def test_all_ok_without_items_is_empty(self):
        import policy_live
        self.assertEqual(policy_live._merge_status("empty", "empty"), "empty")

    def test_search_with_status_carries_detail(self):
        import policy_live
        item = {"title": "公司公告 A", "relation_type": "direct", "published_at": "2026-11-01"}
        with mock.patch.object(policy_live, "fetch_announcements",
                               return_value=([item], "success")), \
             mock.patch.object(policy_live, "fetch_news", return_value=([], "partial")):
            items, status, detail = policy_live.search_with_status("600519", "贵州茅台", "白酒")
        self.assertEqual(len(items), 1)
        self.assertEqual(status, "partial")
        self.assertEqual(detail["announcements"], "success")
        self.assertEqual(detail["news"], "partial")

    def test_search_dedupes_same_title(self):
        import policy_live
        a = {"title": "同一 条", "relation_type": "direct", "published_at": "2026-11-01"}
        b = {"title": "同一条", "relation_type": "direct", "published_at": "2026-11-02"}
        with mock.patch.object(policy_live, "fetch_announcements", return_value=([a], "success")), \
             mock.patch.object(policy_live, "fetch_news", return_value=([b], "success")):
            items, _status, _d = policy_live.search_with_status("600519", "贵州茅台", "白酒")
        self.assertEqual(len(items), 1, "空白差异的同名条目只保留一条")


class TestKeywordEvidence(unittest.TestCase):
    """关键词必须来自真实经营范围，且能指回原文。"""

    def test_connector_phrase_is_split(self):
        import stock_profile
        kw, ev = stock_profile._terms_with_evidence(
            "锂离子电池以及其他电池、充电器、电子产品的生产、销售")
        self.assertIn("锂离子电池", kw)
        self.assertIn("充电器", kw)
        self.assertNotIn("锂离子电池以及其他电池", kw, "不能把整句当成关键词")

    def test_keyword_has_evidence(self):
        import stock_profile
        kw, ev = stock_profile._terms_with_evidence("茅台酒及系列酒的生产与销售")
        self.assertIn("茅台酒", kw)
        self.assertTrue(ev)
        for e in ev:
            self.assertIn(e["keyword"], kw)
            self.assertTrue(e["text"], "每个关键词都要有出处片段")
            self.assertTrue(e["source"])

    def test_generic_terms_dropped(self):
        import stock_profile
        kw, _ = stock_profile._terms_with_evidence("其他电池、产品、业务、技术")
        for bad in ("其他电池", "产品", "业务", "技术"):
            self.assertNotIn(bad, kw)

    def test_empty_profile_has_no_fake_business(self):
        import stock_profile
        p = stock_profile.empty_profile("600519", industry="白酒")
        self.assertEqual(p["business"], "")
        self.assertEqual(p["keyword_evidence"], [])
        self.assertIn("business", p["missing"])


class TestDataHealth(unittest.TestCase):
    """3.2/3.3：不联网、状态按真实时间、单模块失败不牵连。"""

    def test_snapshot_does_not_touch_network(self):
        import data_health
        with mock.patch("requests.get", side_effect=AssertionError("不许联网")), \
             mock.patch("requests.Session", side_effect=AssertionError("不许联网")):
            snap = data_health.snapshot()
        self.assertIn("rows", snap)
        self.assertGreaterEqual(len(snap["rows"]), 8)

    def test_every_row_has_required_fields(self):
        import data_health
        snap = data_health.snapshot()
        for row in snap["rows"]:
            for k in ("module", "status", "updated_at", "source", "detail", "ok"):
                self.assertIn(k, row)
            self.assertIn(row["status"], ("REAL", "CACHED", "STALE", "PARTIAL", "UNAVAILABLE"))

    def test_stale_when_timestamp_missing(self):
        import data_health
        status, why = data_health._age_status(None)
        self.assertEqual(status, "STALE")
        self.assertIn("无法确认", why)

    def test_real_within_15min(self):
        import data_health
        import time
        self.assertEqual(data_health._age_status(time.time())[0], "REAL")

    def test_cached_within_a_day(self):
        import data_health
        import time
        self.assertEqual(data_health._age_status(time.time() - 3600)[0], "CACHED")

    def test_broken_policy_db_does_not_crash(self):
        import data_health
        with mock.patch("events_store.connect", side_effect=RuntimeError("db locked")):
            row = data_health._policy_row()
        self.assertEqual(row["status"], "UNAVAILABLE")
        self.assertIn("读取失败", row["detail"])

    def test_api_endpoint_ok(self):
        from app import app
        c = app.test_client()
        r = c.get("/api/data-health")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["code"], 200)
        self.assertIn("summary", body["data"])


class TestPolicyIndustryOrder(unittest.TestCase):
    """2.2 的核心回归：行业名必须在库查询之前解析好。

    旧实现 live=1 时跳过 get_industry_pe，库查询拿到的 industry 是空字符串，
    行业/产业链政策被整段漏掉。
    """

    def test_db_query_receives_industry_even_when_profile_fails(self):
        import routes, events_store
        from app import app

        seen = {}

        def fake_pe(code, **kw):
            return {"pe": 20.0, "industry": "白酒", "source": "eastmoney"}

        def fake_policy(code, stock_name="", industry_name=""):
            seen["industry"] = industry_name
            return []

        c = app.test_client()
        with mock.patch.object(routes, "get_industry_pe", side_effect=fake_pe), \
             mock.patch.object(events_store, "policy_events_for", side_effect=fake_policy), \
             mock.patch("stock_profile.profile_for", side_effect=RuntimeError("f10 down")):
            r = c.get("/api/policy-events?stock_code=600519&live=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen.get("industry"), "白酒", "live 请求也必须带上行业名查库")
        meta = r.get_json()["data"]["meta"]
        self.assertEqual(meta["industry_source"], "eastmoney")
        self.assertEqual(meta["profile_status"].split(":")[0], "failed")


if __name__ == "__main__":
    unittest.main()
