# -*- coding: utf-8 -*-
"""V2.0.1 P1：政策自动检索 + 个股关联分析闭环

三条底线（审查口径）：
  1. 个股画像只能来自可验证来源；取不到就标"资料缺失"，不许凭空写主营业务
  2. 关联类型必须分档：direct / business_related / industry_indirect / macro_related / unconfirmed
  3. 自动检索结果一律"待确认"，不许把行业利好直接说成个股利好，也不许自动判涨跌
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask                     # noqa: E402

import events_store                         # noqa: E402
import policy_live                          # noqa: E402
import routes                               # noqa: E402
import stock_profile                        # noqa: E402


F10_OK = {"jbzl": [{
    "SECURITY_CODE": "600519",
    "SECURITY_NAME_ABBR": "贵州茅台",
    "EM2016": "食品饮料-饮料-白酒",
    "BUSINESS_SCOPE": "茅台酒及系列酒的生产与销售;饮料、食品、包装材料的生产、销售;防伪技术开发",
    "ORG_PROFILE": "贵州茅台酒股份有限公司主营茅台酒及系列酒的生产与销售。",
}]}


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


class TestStockProfile(unittest.TestCase):
    def test_profile_from_f10(self):
        with mock.patch.object(stock_profile.requests, "get", return_value=FakeResp(F10_OK)):
            p = stock_profile.fetch_profile("600519")
        self.assertEqual(p["name"], "贵州茅台")
        self.assertEqual(p["industry"], "白酒")
        self.assertEqual(p["industry_chain"], ["食品饮料", "饮料", "白酒"])
        self.assertIn("茅台酒及系列酒的生产与销售", p["business"])
        self.assertEqual(p["missing"], [])
        self.assertTrue(p["source_url"].endswith("SH600519"))
        # 关键词必须来自经营范围原文（可回溯），不能是联想出来的
        self.assertTrue(p["keywords"])
        for kw in p["keywords"]:
            self.assertIn(kw, p["business"] + p["industry"])

    def test_failure_marks_missing_not_fabricated(self):
        with mock.patch.object(stock_profile.requests, "get", side_effect=TimeoutError("boom")):
            p = stock_profile.fetch_profile("600519")
        self.assertEqual(p["business"], "")
        self.assertIn("business", p["missing"])
        self.assertIn("industry_chain", p["missing"])
        self.assertTrue(p["note"])
        # 没有任何关键词能凭空指向具体业务
        self.assertEqual(p["keywords"], [])

    def test_empty_business_still_marked(self):
        payload = {"jbzl": [{"SECURITY_CODE": "600519", "SECURITY_NAME_ABBR": "贵州茅台",
                             "EM2016": "食品饮料-饮料-白酒", "BUSINESS_SCOPE": ""}]}
        with mock.patch.object(stock_profile.requests, "get", return_value=FakeResp(payload)):
            p = stock_profile.fetch_profile("600519")
        self.assertEqual(p["business"], "")
        self.assertIn("business", p["missing"])
        self.assertEqual(p["industry"], "白酒")          # 行业链仍然来自真实分类

    def test_bad_code_returns_empty_profile(self):
        p = stock_profile.fetch_profile("abc")
        self.assertIn("business", p["missing"])

    def test_cache_avoids_second_request(self):
        stock_profile.clear_cache()
        with mock.patch.object(stock_profile.requests, "get", return_value=FakeResp(F10_OK)) as m:
            stock_profile.profile_for("600519")
            stock_profile.profile_for("600519")
        self.assertEqual(m.call_count, 1, "6 小时缓存没生效，每开一次页面都打外网")
        stock_profile.clear_cache()


class TestRelationClassification(unittest.TestCase):
    KW = ["茅台酒", "系列酒"]

    def test_company_name_is_unconfirmed(self):
        rel, why, _ = policy_live._classify_title("贵州茅台发布三季报", "贵州茅台", "白酒", self.KW)
        self.assertEqual(rel, "unconfirmed")
        self.assertIn("公司名", why)

    def test_business_keyword_is_business_related(self):
        rel, why, matched = policy_live._classify_title("茅台酒出厂价上调传闻", "", "白酒", self.KW)
        self.assertEqual(rel, "business_related")
        self.assertEqual(matched, "茅台酒")
        self.assertIn("经营范围", why)

    def test_industry_only_is_industry_indirect(self):
        rel, _, matched = policy_live._classify_title("食品饮料行业迎新政", "", "食品饮料", [])
        self.assertEqual(rel, "industry_indirect")
        self.assertEqual(matched, "食品饮料")

    def test_unrelated_title_dropped(self):
        rel, why, _ = policy_live._classify_title("某地暴雨橙色预警", "贵州茅台", "白酒", self.KW)
        self.assertIsNone(rel)
        self.assertEqual(why, "")

    def test_labels_cover_all_types(self):
        for rel in ("direct", "business_related", "industry_indirect", "macro_related", "unconfirmed"):
            self.assertIn(rel, policy_live.RELATION_LABEL)

    def test_search_orders_by_relation_strength_and_dedupes(self):
        ann = [{"title": "董事会决议", "relation_type": "direct", "published_at": "2026-11-01"}]
        news = [
            {"title": "行业新政发布", "relation_type": "industry_indirect", "published_at": "2026-11-05"},
            {"title": "董事会决议 ", "relation_type": "direct", "published_at": "2026-11-01"},  # 重复
            {"title": "白酒提价", "relation_type": "business_related", "published_at": "2026-11-03"},
        ]
        with mock.patch.object(policy_live, "fetch_announcements", return_value=ann), \
                mock.patch.object(policy_live, "fetch_news", return_value=news):
            out = policy_live.search("600519", "贵州茅台", "白酒", limit=10, profile={"keywords": ["白酒"]})
        self.assertEqual([x["relation_type"] for x in out],
                         ["direct", "business_related", "industry_indirect"])


class TestPolicyApiProfile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="policy-auto-")
        self._env = mock.patch.dict(os.environ, {"STOCK_EVENTS_DB": os.path.join(self.tmpdir, "e.db")})
        self._env.start()
        app = Flask(__name__)
        app.register_blueprint(routes.bp)
        self.client = app.test_client()

    def tearDown(self):
        self._env.stop()
        for f in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)

    def _get(self, code, name, profile, events):
        with mock.patch.object(stock_profile, "profile_for", return_value=profile), \
                mock.patch.object(policy_live, "search", return_value=events):
            r = self.client.get(f"/api/policy-events?stock_code={code}&stock_name={name}&live=1")
        return r.get_json()

    def test_profile_returned_and_pending(self):
        prof = {"code": "600519", "name": "贵州茅台", "industry": "白酒",
                "industry_chain": ["食品饮料", "饮料", "白酒"], "business": "茅台酒生产销售",
                "business_short": "茅台酒生产销售", "keywords": ["白酒"], "missing": [],
                "source": "东方财富 F10·公司概况", "source_url": "https://x"}
        ev = {"title": "公告A", "relation_type": "direct", "relation_label": "直接影响（公司公告）",
              "direction": "待确认", "impact_level": "待确认", "mechanism": "m",
              "relation_basis": "b", "evidence": "e", "review_status": "pending",
              "source_url": "https://e", "published_at": "2026-11-01"}
        body = self._get("600519", "贵州茅台", prof, [ev])
        self.assertEqual(body["code"], 200)
        data = body["data"]
        self.assertEqual(data["profile"]["industry"], "白酒")
        self.assertEqual(data["meta"]["profile_status"], "ok")
        self.assertEqual(data["events"][0]["review_status"], "pending")
        self.assertEqual(data["events"][0]["direction"], "待确认")

    def test_partial_profile_flagged(self):
        prof = stock_profile.empty_profile("600519", industry="白酒")
        body = self._get("600519", "贵州茅台", prof, [])
        self.assertEqual(body["data"]["meta"]["profile_status"], "partial")
        self.assertIn("business", body["data"]["meta"]["profile_missing"])

    def test_profile_failure_does_not_break_endpoint(self):
        with mock.patch.object(stock_profile, "profile_for", side_effect=RuntimeError("boom")), \
                mock.patch.object(policy_live, "search", return_value=[]):
            r = self.client.get("/api/policy-events?stock_code=600519&live=1")
        body = r.get_json()
        self.assertEqual(body["code"], 200)
        self.assertIn("failed", body["data"]["meta"]["profile_status"])

    def test_three_stocks_get_different_events(self):
        """三只业务差异明显的股票不能拿到同一批事件。"""
        profs = {
            "600519": {"code": "600519", "name": "贵州茅台", "industry": "白酒",
                       "business_short": "茅台酒", "keywords": ["白酒"], "missing": []},
            "601899": {"code": "601899", "name": "紫金矿业", "industry": "有色金属",
                       "business_short": "铜金矿", "keywords": ["铜"], "missing": []},
            "002594": {"code": "002594", "name": "比亚迪", "industry": "汽车整车",
                       "business_short": "新能源车", "keywords": ["新能源车"], "missing": []},
        }
        seen = {}
        for code, prof in profs.items():
            ev = {"title": f"公告-{code}", "relation_type": "direct", "review_status": "pending",
                  "direction": "待确认", "impact_level": "待确认", "source_url": "https://e"}
            body = self._get(code, prof["name"], prof, [ev])
            seen[code] = {e["title"] for e in body["data"]["events"]}
        all_titles = [t for s in seen.values() for t in s]
        self.assertEqual(len(all_titles), len(set(all_titles)), "三只股票出现了相同事件")


if __name__ == "__main__":
    unittest.main()
