# -*- coding: utf-8 -*-
"""S0（V2.1 起点）：上证指数后面的日期不再显示 undefined

起因：market_flow.html 写 `${d.index_name}  ${d.date}`，但 /api/market-flow 返回的
data 里没有 date —— build_market_flow_chart() 把真实交易日只拼进了 title，
页面就渲染成「上证指数 undefined」。真实浏览器里复现过（3649.18 上证指数 undefined -1.10%）。

修法（不生成任何日期）：
  1. get_market_flow() 解析腾讯行情时本来就得到 date（8 位，如 20261106）；
  2. build_market_flow_chart() 原样把它透出成 data.date；
  3. 前端用 window.fmt.date8() 格式化，取不到就整段不显示。

这里锁三件事：字段确实透出、取不到时是空串而不是垃圾值、模板不再裸插 d.date。
"""

import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask            # noqa: E402

import chart_builder               # noqa: E402
import data_service as d           # noqa: E402
import routes                      # noqa: E402

TEMPLATE = os.path.join(ROOT, "templates", "market_flow.html")


def tencent_index_text(trade_time="20261106150000", name="上证指数",
                       price="3649.18", change="-1.1"):
    """腾讯行情报文（~ 分隔）。parts[1]=名称、[3]=现价、[30]=时间、[32]=涨跌幅。"""
    parts = [""] * 60
    parts[0] = "1"
    parts[1] = name
    parts[2] = "000001"
    parts[3] = price
    parts[30] = trade_time
    parts[32] = change
    return "~".join(parts)


def eastmoney_flow_ok():
    return {"data": {"diff": [{"f62": 3.2e8, "f66": 1.1e8, "f69": -2.0e7,
                               "f72": 8.0e7, "f75": -5.0e7},
                              {"f62": -1.2e8, "f66": -4.0e7, "f69": 1.0e7,
                               "f72": -3.0e7, "f75": 2.0e7}]}}


class Resp:
    def __init__(self, text="", payload=None, status=200):
        self.text = text if text else json.dumps(payload or {})
        self.status_code = status

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class MarketFlowDateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = d._CACHE_FILE
        self._status = d._market_status
        self._last = dict(d._LAST_SUCCESS)
        d._CACHE_FILE = os.path.join(self.tmp.name, "cache.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        d._market_status = lambda: ("交易中", True)

        self.app = Flask(__name__)
        self.app.register_blueprint(routes.bp)
        self.client = self.app.test_client()

    def tearDown(self):
        d._CACHE_FILE = self._cache
        d._market_status = self._status
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._LAST_SUCCESS.update(self._last)
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        self.tmp.cleanup()

    def use_http(self, tencent_text, em_payload=None):
        """腾讯与东财都在 get_market_flow 里被请求：按域名分流。"""
        def fake_get(url, **kw):
            if "qt.gtimg" in url or "tencent" in url:
                return Resp(text=tencent_text)
            return Resp(payload=em_payload if em_payload is not None else eastmoney_flow_ok())
        return mock.patch.object(d.requests, "get", side_effect=fake_get)


class TestChartPayloadHasDate(unittest.TestCase):
    """build_market_flow_chart：真实交易日原样透出，不许凭空生成。"""

    def _flow(self, **over):
        base = {"main_force": 1.0, "super_large": 0.6, "large_order": 0.4,
                "middle_order": -0.2, "small_order": -0.8,
                "date": "20261106", "index_name": "上证指数",
                "index_price": 3649.18, "index_change": -1.1}
        base.update(over)
        return base

    def test_date_passthrough(self):
        chart = chart_builder.build_market_flow_chart(self._flow())
        assert chart is not None
        self.assertEqual(chart["date"], "20261106",
                         "真实交易日必须原样透出给前端")

    def test_date_missing_is_empty_not_fabricated(self):
        chart = chart_builder.build_market_flow_chart(self._flow(date=""))
        assert chart is not None
        self.assertEqual(chart["date"], "")
        self.assertIsInstance(chart["date"], str)
        # 空日期时标题不留一个空括号
        self.assertEqual(chart["title"], "全市资金流向")

    def test_date_none_is_empty(self):
        chart = chart_builder.build_market_flow_chart(self._flow(date=None))
        assert chart is not None
        self.assertEqual(chart["date"], "")

    def test_other_fields_unchanged(self):
        """透出 date 不能顺手改掉其它字段（页面/图表都依赖）。"""
        chart = chart_builder.build_market_flow_chart(self._flow())
        assert chart is not None
        for k in ("main_force", "index_name", "index_price", "index_change", "items", "colors"):
            self.assertIn(k, chart)
        self.assertEqual(chart["index_name"], "上证指数")
        self.assertEqual([i["name"] for i in chart["items"]],
                         ["超大单", "大单", "中单", "小单"])

    def test_none_input_still_none(self):
        self.assertIsNone(chart_builder.build_market_flow_chart(None))


class TestMarketFlowApiDate(MarketFlowDateBase):
    """/api/market-flow 的 data 里必须有 date，且等于真实交易日。"""

    def test_api_exposes_date(self):
        with self.use_http(tencent_index_text("20261106150000")):
            r = self.client.get("/api/market-flow")
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["code"], 200)
        self.assertIn("date", body["data"], "data 里必须有 date 字段，否则前端只能渲染 undefined")
        self.assertEqual(body["data"]["date"], "20261106")
        self.assertEqual(body["data"]["index_name"], "上证指数")

    def test_api_has_no_undefined_strings(self):
        with self.use_http(tencent_index_text("20261106150000")):
            raw = self.client.get("/api/market-flow").get_data(as_text=True)
        self.assertNotIn("undefined", raw)

    def test_api_date_empty_when_source_omits_time(self):
        """源不返回时间字段时：date 为空串，不能编一个当天日期出来。"""
        with self.use_http(tencent_index_text("")):
            body = self.client.get("/api/market-flow").get_json()
        self.assertEqual(body["data"]["date"], "")


class TestTemplateNoBareDate(unittest.TestCase):
    """模板不允许再裸插 d.date：必须经 fmt.date8 守卫。"""

    def setUp(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            self.src = f.read()

    def test_no_bare_date_interpolation(self):
        bad = re.findall(r"\$\{[^}]*\bd\.date\b[^}]*\}", self.src)
        self.assertEqual(
            [b for b in bad if "date8" not in b], [],
            "模板里仍有裸插 d.date 的表达式，缺字段时会渲染 undefined")

    def test_uses_guarded_formatter(self):
        self.assertIn("fmt.date8(d.date)", self.src,
                      "指数卡片必须用 fmt.date8 处理交易日")
        self.assertRegex(self.src, r"asof \? '  ' \+ asof : ''",
                         "日期为空时应该整段不显示，而不是留占位")

    def test_label_shows_name_and_date(self):
        """基础语义没变：名称 + 日期，少了个日期也不至于只剩价格。"""
        self.assertIn("${d.index_name}", self.src)


if __name__ == "__main__":
    unittest.main()
