# -*- coding: utf-8 -*-
"""S3-4A：东方财富 F10 财报 Provider 专项测试。

全部离线：S3-0 真实报文（tests/fixtures/s3_0/em_f10_600519.raw）回放 +
脚本化 `_get()` 模拟网络/结构异常。不发真实上游请求。

分组（对应授权文档 §九）：
  A  真实报文十字段逐字段对照（旧解析逻辑只在本测试内作为对照，不进生产）
  B  null / 真实零 语义
  C  empty / failed 分类（HTTP 500 / 超时 / 连接失败 / 非法 JSON / 结构错误）
  D  市场后缀：600519/601398/688981/000001/300750/920001 + 北交所补充 + 非法输入
  E  缓存：命中 / 6 小时 TTL / refresh / 失败不缓存 / empty 不缓存 / 旧缓存兼容 / 键隔离
  F  业务回归：financials 结构、内在价值（正常/0/负/None/无财报/失败）、HTTP 接口 200
"""

import copy
import json
import math
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import data_service as d                                              # noqa: E402
from services.data.base import ProviderResult                        # noqa: E402
from services.data.normalizers import stock_secucode, secid           # noqa: E402
from services.data.providers.eastmoney_f10 import (                   # noqa: E402
    EastMoneyF10Provider, FIELD_MAP, PROVIDER_NAME, _date_field, _num_field,
)

FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures", "s3_0")
RAW_PATH = os.path.join(FIXTURE_DIR, "em_f10_600519.raw")
META_PATH = os.path.join(FIXTURE_DIR, "em_f10_600519.meta.json")


def load_raw():
    with open(RAW_PATH, encoding="utf-8") as f:
        return json.load(f)


def old_parser(row):
    """迁移前 data_service 里的解析逻辑，只用于逐字段对照。"""
    return {
        "report_date": (row.get("REPORT_DATE") or "")[:10],
        "report_name": row.get("REPORT_DATE_NAME") or "",
        "eps": row.get("EPSJB"),
        "bps": row.get("BPS"),
        "revenue": row.get("TOTALOPERATEREVE"),
        "revenue_yoy": row.get("TOTALOPERATEREVETZ"),
        "net_profit": row.get("PARENTNETPROFIT"),
        "profit_yoy": row.get("PARENTNETPROFITTZ"),
        "roe": row.get("ROEJQ"),
        "notice_date": (row.get("NOTICE_DATE") or "")[:10],
    }


class FakeF10(EastMoneyF10Provider):
    """把 `_get()` 换成脚本化响应；记录收到的 params。"""

    def __init__(self, payload=None, exc=None):
        super().__init__()
        self.payload = payload
        self.exc = exc
        self.calls = []

    def _get(self, params):
        self.calls.append(params)
        if self.exc:
            raise self.exc
        return self.payload


_BIZ_TO_F10 = {biz: f10 for biz, f10, _ in FIELD_MAP}


def payload_with(**overrides):
    """以真实报文为底，覆盖 data[0] 的字段（改的是内存副本，不动 fixture 文件）。

    可以用业务字段名（eps=...）或原始字段名（EPSJB=...）覆盖。
    """
    raw = load_raw()
    row = dict(raw["result"]["data"][0])
    for key, value in overrides.items():
        row[_BIZ_TO_F10.get(key, key)] = value
    raw["result"]["data"] = [row]
    return raw


def patched_get(exc=None, status=200, body=None, json_exc=None):
    """替换 Provider 模块内的 requests.get（不是 _get，确保异常映射真的被执行）。"""
    target = "services.data.providers.eastmoney_f10.requests.get"
    if exc is not None:
        return mock.patch(target, side_effect=exc)
    resp = mock.Mock()
    resp.status_code = status
    if json_exc is not None:
        resp.json.side_effect = json_exc
    else:
        resp.json.return_value = body
    return mock.patch(target, return_value=resp)


class TestGroupA_RealFixtureFields(unittest.TestCase):
    """A：真实报文 10/10 逐字段一致（值、类型、日期都一致）。"""

    @classmethod
    def setUpClass(cls):
        cls.raw = load_raw()
        cls.row = cls.raw["result"]["data"][0]
        cls.data, cls.asof, cls.extra = EastMoneyF10Provider().normalize(cls.raw, "600519")

    def test_fixture_integrity(self):
        """夹具没被改过：字节数与 SHA256 与 meta 一致。"""
        import hashlib
        with open(RAW_PATH, "rb") as f:
            blob = f.read()
        with open(META_PATH, encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(len(blob), meta["bytes"])
        self.assertEqual(hashlib.sha256(blob).hexdigest(), meta["sha256"])

    def test_ten_keys_exact(self):
        self.assertEqual([k for k, _, _ in FIELD_MAP],
                         ["report_date", "report_name", "eps", "bps", "revenue",
                          "revenue_yoy", "net_profit", "profit_yoy", "roe", "notice_date"])
        self.assertEqual(sorted(self.data.keys()), sorted([k for k, _, _ in FIELD_MAP]))

    def test_field_by_field_matches_old_parser(self):
        expected = old_parser(self.row)
        for key, want in expected.items():
            got = self.data[key]
            self.assertEqual(got, want, f"{key} 值不一致")
            self.assertIs(type(got), type(want), f"{key} 类型不一致：{type(got)} vs {type(want)}")

    def test_report_and_notice_dates(self):
        self.assertEqual(self.data["report_date"], "2026-09-30")
        self.assertEqual(self.data["notice_date"], "2026-10-31")
        self.assertEqual(self.asof, "2026-09-30")     # 数据自身时间 = 财报期，不是抓取时刻

    def test_values_not_rounded_or_rescaled(self):
        self.assertEqual(self.data["bps"], 216.949629787912)   # 没被 round(2)
        self.assertEqual(self.data["revenue"], 133283652508.95)  # 仍是元，没转亿元
        self.assertEqual(self.data["revenue_yoy"], 1.817946648268)
        self.assertEqual(self.data["profit_yoy"], -0.246524447439)

    def test_extra_has_source_facts(self):
        self.assertEqual(self.extra["secucode"], "600519.SH")
        self.assertEqual(self.extra["row_fields"], 165)
        self.assertEqual(self.extra["report_type"], "三季报")


class TestGroupB_NullAndZero(unittest.TestCase):
    """B：null 是 null，零是零 —— 不允许 `value or 0`。"""

    def setUp(self):
        self.row = load_raw()["result"]["data"][0]

    def test_fixture_facts(self):
        nulls = sum(1 for v in self.row.values() if v is None)
        self.assertEqual(nulls, 56)
        self.assertIsNone(self.row["EPSKCJB"])
        self.assertEqual(self.row["EPSJBTZ"], 0)
        self.assertEqual(self.row["B_FREE_SHARE"], 0)

    def test_raw_null_stays_none(self):
        self.assertIsNone(_num_field(self.row, "EPSKCJB"))
        self.assertIsNone(_num_field(self.row, "EPSJBTZ_NOPE"))

    def test_raw_zero_stays_zero(self):
        self.assertEqual(_num_field(self.row, "EPSJBTZ"), 0)
        self.assertEqual(_num_field(self.row, "B_FREE_SHARE"), 0)
        self.assertIsInstance(_num_field(self.row, "EPSJBTZ"), int)

    def test_business_fields_null_and_zero(self):
        raw = payload_with(eps=None, bps=None, revenue=0, net_profit=0,
                           revenue_yoy=-12.5, roe=None)
        data, _, _ = EastMoneyF10Provider().normalize(raw, "600519")
        self.assertIsNone(data["eps"])
        self.assertIsNone(data["bps"])
        self.assertEqual(data["revenue"], 0)          # 真实 0，不是 None
        self.assertEqual(data["net_profit"], 0)
        self.assertEqual(data["revenue_yoy"], -12.5)  # 负数同比保持负
        self.assertIsNone(data["roe"])

    def test_dash_and_empty_string_are_none(self):
        raw = payload_with(eps="-", bps="", roe="--")
        data, _, _ = EastMoneyF10Provider().normalize(raw, "600519")
        self.assertIsNone(data["eps"])
        self.assertIsNone(data["bps"])
        self.assertIsNone(data["roe"])

    def test_numeric_string_is_coerced(self):
        raw = payload_with(eps="51.53", revenue="133283652508.95")
        data, _, _ = EastMoneyF10Provider().normalize(raw, "600519")
        self.assertEqual(data["eps"], 51.53)
        self.assertEqual(data["revenue"], 133283652508.95)

    def test_unparsable_value_is_format_error(self):
        raw = payload_with(eps="abc")
        with self.assertRaises(Exception) as ctx:
            EastMoneyF10Provider().normalize(raw, "600519")
        self.assertEqual(ctx.exception.status, "failed")
        self.assertIn("field=EPSJB", ctx.exception.message)


class TestGroupC_EmptyVsFailed(unittest.TestCase):
    """C：empty 与 failed 必须分开。"""

    def test_empty_list_is_empty_status(self):
        res = FakeF10({"result": {"data": []}, "success": True}).fetch("600519")
        self.assertEqual(res.status, "empty")
        self.assertFalse(res.ok())
        self.assertIsNone(res.data)

    def test_data_null_is_empty_status(self):
        res = FakeF10({"result": {"data": None}, "success": True}).fetch("600519")
        self.assertEqual(res.status, "empty")

    def test_result_missing_is_failed(self):
        res = FakeF10({"success": True}).fetch("600519")
        self.assertEqual(res.status, "failed")
        self.assertIn("result", res.error["message"])

    def test_result_wrong_type_is_failed(self):
        res = FakeF10({"result": [], "success": True}).fetch("600519")
        self.assertEqual(res.status, "failed")
        self.assertIn("result 不是对象", res.error["message"])

    def test_data_wrong_type_is_failed(self):
        res = FakeF10({"result": {"data": {"x": 1}}, "success": True}).fetch("600519")
        self.assertEqual(res.status, "failed")
        self.assertIn("不是列表", res.error["message"])

    def test_row_not_object_is_failed(self):
        res = FakeF10({"result": {"data": ["oops"]}, "success": True}).fetch("600519")
        self.assertEqual(res.status, "failed")
        self.assertIn("data[0]", res.error["message"])

    def test_upstream_success_false_is_failed(self):
        res = FakeF10({"success": False, "code": 500, "message": "boom",
                       "result": None}).fetch("600519")
        self.assertEqual(res.status, "failed")

    def test_http_500_is_failed(self):
        with patched_get(status=500, body={}):
            res = EastMoneyF10Provider().fetch("600519")
        self.assertEqual(res.status, "failed")
        self.assertIn("HTTP 500", res.error["message"])

    def test_timeout_is_failed(self):
        with patched_get(exc=requests.Timeout("slow")):
            res = EastMoneyF10Provider().fetch("600519")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderTimeout")

    def test_connection_error_is_failed(self):
        with patched_get(exc=requests.ConnectionError("refused")):
            res = EastMoneyF10Provider().fetch("600519")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderNetworkError")

    def test_bad_json_is_failed(self):
        with patched_get(json_exc=ValueError("No JSON object could be decoded")):
            res = EastMoneyF10Provider().fetch("600519")
        self.assertEqual(res.status, "failed")
        self.assertIn("JSON", res.error["message"])

    def test_missing_report_date_is_failed_with_field_name(self):
        raw = payload_with(REPORT_DATE=None)
        res = FakeF10(raw).fetch("600519")
        self.assertEqual(res.status, "failed")
        self.assertIn("field=REPORT_DATE", res.error["message"])

    def test_invalid_report_date_is_failed(self):
        raw = payload_with(REPORT_DATE="2026-13-99 00:00:00")
        res = FakeF10(raw).fetch("600519")
        self.assertEqual(res.status, "failed")
        self.assertIn("field=REPORT_DATE", res.error["message"])

    def test_optional_nulls_do_not_fail(self):
        raw = payload_with(eps=None, roe=None, bps=None)
        res = FakeF10(raw).fetch("600519")
        self.assertEqual(res.status, "success")
        self.assertIsNone(res.data["eps"])

    def test_status_dimensions_for_empty_and_failed(self):
        empty = FakeF10({"result": {"data": []}}).fetch("600519")
        self.assertEqual(empty.dimensions()["fetch_status"], "empty")
        bad = FakeF10({"result": None}).fetch("600519")
        self.assertEqual(bad.dimensions()["fetch_status"], "failed")


class TestGroupD_MarketSuffix(unittest.TestCase):
    """D：市场后缀唯一来源 + 完整 F10 filter 字符串。"""

    CASES = {
        "600519": ("600519.SH", "1.600519"),
        "601398": ("601398.SH", "1.601398"),
        "688981": ("688981.SH", "1.688981"),
        "000001": ("000001.SZ", "0.000001"),
        "300750": ("300750.SZ", "0.300750"),
        "920001": ("920001.BJ", "0.920001"),
    }

    def test_six_standard_codes(self):
        for code, (secucode, secid_want) in self.CASES.items():
            self.assertEqual(stock_secucode(code), secucode, code)
            self.assertEqual(secid(code), secid_want, code)

    def test_bj_segments(self):
        for code in ("430047", "830799", "870204", "880001", "920002"):
            self.assertTrue(stock_secucode(code).endswith(".BJ"), code)

    def test_secid_index_vs_stock_same_code(self):
        """000001 作为股票是平安银行(.SZ)，作为指数是上证指数(1.000001)。"""
        self.assertEqual(secid("000001", "stock"), "0.000001")
        self.assertEqual(secid("000001", "index"), "1.000001")
        self.assertEqual(stock_secucode("000001"), "000001.SZ")

    def test_invalid_codes_rejected(self):
        for bad in ("60051", "6005199", "abcdef", "", "777777"):
            with self.assertRaises(ValueError, msg=bad):
                stock_secucode(bad)

    def test_full_filter_string(self):
        prov = FakeF10(load_raw())
        params = prov.build_params("600519")
        self.assertEqual(params["filter"], '(SECUCODE="600519.SH")')
        self.assertEqual(params["reportName"], "RPT_F10_FINANCE_MAINFINADATA")
        self.assertEqual(params["pageSize"], "1")
        self.assertEqual((params["sortColumns"], params["sortTypes"]), ("REPORT_DATE", "-1"))

    def test_filter_for_sz_and_bj(self):
        prov = FakeF10(load_raw())
        self.assertEqual(prov.build_params("000001")["filter"], '(SECUCODE="000001.SZ")')
        self.assertEqual(prov.build_params("920001")["filter"], '(SECUCODE="920001.BJ")')

    def test_only_one_request_path(self):
        """请求只存在于 Provider；data_service 里不许再出现 requests.get。"""
        import inspect
        import services.data.providers.eastmoney_f10 as mod
        self.assertEqual(mod.URL, "https://datacenter-web.eastmoney.com/api/data/v1/get")
        src = inspect.getsource(d._fin_get_with_cache)
        self.assertNotIn("requests.get", src)
        self.assertNotIn('SECUCODE="', src)
        self.assertIn("eastmoney_f10", src)

    def test_illegal_code_fails_before_request(self):
        prov = FakeF10(load_raw())
        res = prov.fetch("777777")
        self.assertEqual(res.status, "failed")
        self.assertIn("非法股票代码", res.error["message"])
        self.assertEqual(prov.calls, [])          # 没发请求


class TestGroupE_CacheAndRefresh(unittest.TestCase):
    """E：缓存键 / TTL / refresh / 失败与空不缓存 / 旧缓存兼容。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.remove(self.tmp.name)
        self._orig_file = d._CACHE_FILE
        d._CACHE_FILE = self.tmp.name
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        self.calls = []

    def tearDown(self):
        d._CACHE_FILE = self._orig_file
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def _provider_call(self, payload=None, status="success"):
        def call(name, *args, **kwargs):
            self.calls.append((name, args))
            if status == "success":
                prov = FakeF10(payload if payload is not None else load_raw())
                return prov.fetch(args[0])
            return ProviderResult(None, PROVIDER_NAME, status, "", 0.0, False,
                                  {"message": status})
        return call

    def test_first_call_hits_provider_and_returns_ten_fields(self):
        with mock.patch.object(d._providers, "call", side_effect=self._provider_call()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            fin = d.get_latest_financials("600519")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(fin["report_date"], "2026-09-30")

    def test_second_call_uses_cache(self):
        with mock.patch.object(d._providers, "call", side_effect=self._provider_call()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            first = d.get_latest_financials("600519")
            second = d.get_latest_financials("600519")
        self.assertEqual(len(self.calls), 1, "缓存期内不该重复请求上游")
        self.assertEqual(first["report_date"], second["report_date"])
        self.assertEqual(first["eps"], second["eps"])

    def test_cache_args_ttl_and_force(self):
        captured = []

        def fake_cached(key, fetcher, **kw):
            captured.append((key, kw))
            return {"report_date": "2026-09-30"}

        with mock.patch.object(d._providers, "call", side_effect=self._provider_call()), \
             mock.patch.object(d, "_cached_get", side_effect=fake_cached):
            d.get_latest_financials("600519")
            d.get_latest_financials("600519", refresh=True)

        self.assertEqual(captured[0][0], "fin_600519")
        self.assertEqual(captured[0][1]["ttl"], 6 * 3600)
        self.assertFalse(captured[0][1]["force"])
        self.assertTrue(captured[1][1]["force"], "refresh=True 必须穿透缓存")

    def test_failed_result_not_cached(self):
        with mock.patch.object(d._providers, "call", side_effect=self._provider_call(status="failed")), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            self.assertIsNone(d.get_latest_financials("600519"))
            self.assertIsNone(d.get_latest_financials("600519"))
        self.assertEqual(len(self.calls), 2, "失败不应被缓存成财报")

    def test_empty_result_not_cached(self):
        empty = {"result": {"data": []}, "success": True}
        with mock.patch.object(d._providers, "call", side_effect=self._provider_call(payload=empty)), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            self.assertIsNone(d.get_latest_financials("600519"))
            self.assertIsNone(d.get_latest_financials("600519"))
        self.assertEqual(len(self.calls), 2)

    def test_old_cache_is_still_readable(self):
        old = {"report_date": "2025-12-31", "report_name": "2025年报", "eps": 1.5,
               "bps": 10.0, "revenue": 1e9, "revenue_yoy": 3.0, "net_profit": 2e8,
               "profit_yoy": 4.0, "roe": 12.0, "notice_date": "2026-03-01"}
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"fin_600519": {"__data": old, "fetched_at": __import__("time").time()}}, f)
        with mock.patch.object(d._providers, "call", side_effect=self._provider_call()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            fin = d.get_latest_financials("600519")
        self.assertEqual(fin["report_date"], "2025-12-31", "旧缓存日期不得被改写")
        self.assertEqual(fin["eps"], 1.5)

    def test_cache_keys_isolated_per_stock(self):
        p = payload_with(REPORT_DATE="2026-06-30 00:00:00", REPORT_DATE_NAME="2026中报",
                         EPSJB=2.0)
        with mock.patch.object(d._providers, "call", side_effect=self._provider_call(payload=p)), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            a = d.get_latest_financials("600519")
            b = d.get_latest_financials("000001")
        self.assertEqual(len(self.calls), 2, "不同股票必须分别请求")
        self.assertEqual(a["report_date"], "2026-06-30")

    def test_empty_and_failed_are_distinguished_at_provider(self):
        empty = FakeF10({"result": {"data": []}}).fetch("600519")
        failed = FakeF10({"result": None}).fetch("600519")
        self.assertEqual(empty.status, "empty")
        self.assertEqual(failed.status, "failed")
        self.assertNotEqual(empty.error, failed.error)


class TestGroupF_BusinessRegression(unittest.TestCase):
    """F：financials 结构、内在价值、HTTP 接口。"""

    @classmethod
    def setUpClass(cls):
        cls.fin = EastMoneyF10Provider().normalize(load_raw(), "600519")[0]

    def _quote(self, pe=30.0, price=1500.0):
        return {"code": "600519", "price": price, "pe": pe, "pb": 8.0,
                "name": "贵州茅台", "change": 1.0}

    def test_valuation_financials_structure(self):
        with mock.patch.object(d, "get_stock_quote", return_value=self._quote()), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney", "source_label": "实时", "is_static": False}), \
             mock.patch.object(d, "get_latest_financials", return_value=self.fin), \
             mock.patch.object(d, "get_policy_notes", return_value=[]):
            val = d.get_stock_valuation("600519")
        self.assertEqual(sorted(val["financials"].keys()),
                         sorted([k for k, _, _ in FIELD_MAP]))
        self.assertEqual(val["financials"]["report_date"], "2026-09-30")
        self.assertEqual(val["financials"]["eps"], 51.53)
        self.assertEqual(val["pe"], 30.0)

    def test_intrinsic_value_normal_eps(self):
        """三季报 EPS 51.53 → 年化 ×4/3 → 估值不变。"""
        quote = self._quote()
        with mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}), \
             mock.patch.object(d, "get_latest_financials", return_value=self.fin):
            iv = d.estimate_intrinsic_value(quote)
        self.assertTrue(iv["available"])
        self.assertEqual(iv["model_params"]["raw_eps"], 51.53)
        self.assertEqual(iv["model_params"]["report_date"], "2026-09-30")
        self.assertEqual(iv["model_params"]["annual_factor"], 4 / 3)
        self.assertAlmostEqual(
            iv["intrinsic_value"],
            round(51.53 * 4 / 3, 3) * 25.0 * iv["model_params"]["safety_margin"], places=2)
        self.assertNotIn("eps_annualized", iv["model_params"])   # 不存在的键不要凭空断言
        self.assertAlmostEqual(iv["intrinsic_value"], round(51.53 * 4 / 3, 3) * 25.0 * iv["model_params"]["safety_margin"], places=2)

    def test_intrinsic_value_eps_zero(self):
        quote = self._quote()
        fin = dict(self.fin, eps=0)
        with mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}), \
             mock.patch.object(d, "get_latest_financials", return_value=fin):
            iv = d.estimate_intrinsic_value(quote)
        self.assertFalse(iv["available"])
        self.assertEqual(iv["model_params"]["raw_eps"], 0.0)

    def test_intrinsic_value_eps_negative(self):
        quote = self._quote()
        fin = dict(self.fin, eps=-3.2)
        with mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}), \
             mock.patch.object(d, "get_latest_financials", return_value=fin):
            iv = d.estimate_intrinsic_value(quote)
        self.assertFalse(iv["available"])
        self.assertEqual(iv["model_params"]["raw_eps"], -3.2)

    def test_intrinsic_value_eps_none(self):
        quote = self._quote()
        fin = dict(self.fin, eps=None)
        with mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}), \
             mock.patch.object(d, "get_latest_financials", return_value=fin):
            iv = d.estimate_intrinsic_value(quote)
        self.assertFalse(iv["available"])
        self.assertIsNone(iv["model_params"]["raw_eps"])

    def test_intrinsic_value_no_financials(self):
        quote = self._quote()
        with mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}), \
             mock.patch.object(d, "get_latest_financials", return_value=None):
            iv = d.estimate_intrinsic_value(quote)
        self.assertFalse(iv["available"])
        self.assertIsNone(iv["model_params"]["raw_eps"])

    def test_financials_failure_does_not_change_intrinsic_result(self):
        """财报接口失败（None）与"没有财报"（None）对估值的影响必须一致。"""
        quote = self._quote()
        with mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}), \
             mock.patch.object(d, "get_latest_financials", return_value=None):
            iv_failed = d.estimate_intrinsic_value(quote)
        with mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}), \
             mock.patch.object(d, "get_latest_financials", return_value=None):
            iv_empty = d.estimate_intrinsic_value(quote)
        self.assertEqual(iv_failed["available"], iv_empty["available"])
        self.assertEqual(iv_failed["intrinsic_value"], iv_empty["intrinsic_value"])

    def test_http_endpoints_200_with_financials(self):
        from flask import Flask
        import routes

        app = Flask(__name__)
        app.register_blueprint(routes.bp)
        client = app.test_client()

        with mock.patch.object(d, "get_stock_quote", return_value=self._quote()), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney", "source_label": "实时", "is_static": False}), \
             mock.patch.object(d, "get_latest_financials", return_value=self.fin), \
             mock.patch.object(d, "get_policy_notes", return_value=[]), \
             mock.patch.object(routes, "get_stock_valuation", side_effect=d.get_stock_valuation), \
             mock.patch.object(routes, "get_industry_pe", side_effect=d.get_industry_pe):
            rv = client.get("/api/valuation?code=600519")
            body = rv.get_json()
            rv2 = client.get("/api/intrinsic-value?code=600519")
            body2 = rv2.get_json()

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(body["code"], 200)
        self.assertEqual(sorted(body["data"]["financials"].keys()),
                         sorted([k for k, _, _ in FIELD_MAP]))
        self.assertEqual(rv2.status_code, 200)
        self.assertEqual(body2["code"], 200)
        self.assertIn("model_params", body2["data"])

    def test_no_financials_does_not_500(self):
        from flask import Flask
        import routes

        app = Flask(__name__)
        app.register_blueprint(routes.bp)
        client = app.test_client()
        with mock.patch.object(d, "get_stock_quote", return_value=self._quote()), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney", "source_label": "实时", "is_static": False}), \
             mock.patch.object(d, "get_latest_financials", return_value=None), \
             mock.patch.object(d, "get_policy_notes", return_value=[]), \
             mock.patch.object(routes, "get_stock_valuation", side_effect=d.get_stock_valuation), \
             mock.patch.object(routes, "get_industry_pe", side_effect=d.get_industry_pe):
            rv = client.get("/api/valuation?code=600519")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json()["code"], 200)


def _has_nonfinite(obj):
    """递归检查结构里有没有非有限的浮点数（字符串里出现 "Infinity" 字样不算）。"""
    if isinstance(obj, float):
        return not math.isfinite(obj)
    if isinstance(obj, dict):
        return any(_has_nonfinite(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_has_nonfinite(v) for v in obj)
    return False


def mutate(**overrides):
    """真实报文的深拷贝副本（原 fixture 不动），按业务/原始字段名覆盖 data[0]。"""
    raw = copy.deepcopy(load_raw())
    row = raw["result"]["data"][0]
    for key, value in overrides.items():
        row[_BIZ_TO_F10.get(key, key)] = value
    return raw


class TestGroupG_NonFiniteValues(unittest.TestCase):
    """G：NaN / Infinity / -Infinity / 溢出 一律 failed，不做任何替换。"""

    def _fetch(self, **overrides):
        prov = FakeF10(mutate(**overrides))
        return prov.fetch("600519")

    def test_nan_string_fails(self):
        res = self._fetch(eps="NaN")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderFormatError")
        self.assertIn("field=EPSJB", res.error["message"])

    def test_infinity_string_fails(self):
        res = self._fetch(eps="Infinity")
        self.assertEqual(res.status, "failed")
        self.assertIn("field=EPSJB", res.error["message"])

    def test_negative_infinity_string_fails(self):
        res = self._fetch(eps="-Infinity")
        self.assertEqual(res.status, "failed")
        self.assertIn("field=EPSJB", res.error["message"])

    def test_overflow_string_fails(self):
        res = self._fetch(eps="1e309")
        self.assertEqual(res.status, "failed")
        self.assertIn("field=EPSJB", res.error["message"])

    def test_float_nan_fails(self):
        res = self._fetch(eps=float("nan"))
        self.assertEqual(res.status, "failed")
        self.assertIn("field=EPSJB", res.error["message"])

    def test_float_inf_fails(self):
        res = self._fetch(eps=float("inf"))
        self.assertEqual(res.status, "failed")
        self.assertIn("field=EPSJB", res.error["message"])

    def test_other_numeric_fields_fail(self):
        for field_name in ("TOTALOPERATEREVE", "PARENTNETPROFIT", "BPS", "ROEJQ"):
            value = "NaN" if field_name == "TOTALOPERATEREVE" else "Infinity"
            res = self._fetch(**{field_name: value})
            self.assertEqual(res.status, "failed", field_name)
            self.assertIn(f"field={field_name}", res.error["message"], field_name)

    def test_zero_stays_zero(self):
        res = self._fetch(eps=0)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.data["eps"], 0)
        self.assertIsInstance(res.data["eps"], int)

    def test_null_stays_none(self):
        res = self._fetch(eps=None)
        self.assertEqual(res.status, "success")
        self.assertIsNone(res.data["eps"])

    def test_finite_values_keep_precision(self):
        res = self._fetch(eps=51.53, bps=216.949629787912)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.data["eps"], 51.53)
        self.assertEqual(res.data["bps"], 216.949629787912)

    def test_real_fixture_unaffected(self):
        res = FakeF10(load_raw()).fetch("600519")
        self.assertEqual(res.status, "success")
        self.assertTrue(all(not isinstance(v, float) or math.isfinite(v)
                            for v in res.data.values()))

    def test_nonfinite_eps_never_becomes_intrinsic_value(self):
        """端到端：非有限 EPS 不得产 available=True / inf / nan 的估值。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        os.remove(tmp.name)
        orig = d._CACHE_FILE
        d._CACHE_FILE = tmp.name
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()

        def provider_call(name, *args, **kwargs):
            return FakeF10(mutate(eps="Infinity")).fetch(args[0])

        quote = {"code": "600519", "price": 1500.0, "pe": 30.0, "pb": 8.0, "name": "贵州茅台"}
        try:
            with mock.patch.object(d._providers, "call", side_effect=provider_call), \
                 mock.patch.object(d, "_market_status", return_value=("已收盘", False)), \
                 mock.patch.object(d, "get_industry_pe",
                                   return_value={"pe": 25.0, "industry": "白酒"}):
                self.assertIsNone(d.get_latest_financials("600519"), "非有限值不得当成功财报")
                iv = d.estimate_intrinsic_value(quote)
        finally:
            d._CACHE_FILE = orig
            d._IN_MEMORY_CACHE.clear()
            d._LAST_SUCCESS.clear()
            if os.path.exists(tmp.name):
                os.remove(tmp.name)

        self.assertFalse(iv["available"])
        self.assertIsNone(iv["intrinsic_value"])
        self.assertIsNone(iv["model_params"]["raw_eps"])
        payload = json.dumps(iv, allow_nan=False)   # 严格序列化：不允许 inf/nan 逃逸
        self.assertFalse(_has_nonfinite(json.loads(payload)),
                         "结构里不允许出现非有限浮点数（reason 里引用原始值 'Infinity' 字样不算）")


class TestGroupH_OptionalNoticeDate(unittest.TestCase):
    """H：NOTICE_DATE 可缺失；REPORT_DATE 仍然强制。"""

    def _fetch(self, **overrides):
        return FakeF10(mutate(**overrides)).fetch("600519")

    def test_both_dates_normal(self):
        res = self._fetch()
        self.assertEqual(res.status, "success")
        self.assertEqual(res.data["report_date"], "2026-09-30")
        self.assertEqual(res.data["notice_date"], "2026-10-31")

    def test_notice_null_allows_success(self):
        res = self._fetch(notice_date=None)
        self.assertEqual(res.status, "success")
        self.assertIsNone(res.data["notice_date"])
        self.assertEqual(res.data["eps"], 51.53)

    def test_notice_empty_string_allows_success(self):
        res = self._fetch(notice_date="")
        self.assertEqual(res.status, "success")
        self.assertIsNone(res.data["notice_date"])

    def test_notice_dash_allows_success(self):
        for dash in ("-", "--"):
            res = self._fetch(notice_date=dash)
            self.assertEqual(res.status, "success", dash)
            self.assertIsNone(res.data["notice_date"], dash)

    def test_notice_invalid_nonempty_still_fails(self):
        res = self._fetch(notice_date="2026-13-01")
        self.assertEqual(res.status, "failed")
        self.assertIn("field=NOTICE_DATE", res.error["message"])

    def test_report_date_missing_still_fails(self):
        res = self._fetch(report_date=None)
        self.assertEqual(res.status, "failed")
        self.assertIn("field=REPORT_DATE", res.error["message"])

    def test_report_date_invalid_still_fails(self):
        res = self._fetch(report_date="2026-13-99 00:00:00")
        self.assertEqual(res.status, "failed")
        self.assertIn("field=REPORT_DATE", res.error["message"])

    def test_ten_fields_kept_when_notice_missing(self):
        res = self._fetch(notice_date=None)
        self.assertEqual(sorted(res.data.keys()), sorted([k for k, _, _ in FIELD_MAP]))
        self.assertEqual(len(res.data), 10)

    def test_notice_missing_no_substitution(self):
        res = self._fetch(notice_date=None)
        self.assertIsNone(res.data["notice_date"])          # 不是 ""、不是今天、不是报告期
        self.assertNotEqual(res.data["notice_date"], res.data["report_date"])

    def test_intrinsic_value_still_works_without_notice_date(self):
        fin = self._fetch(notice_date=None).data
        quote = {"code": "600519", "price": 1500.0, "pe": 30.0}
        with mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}), \
             mock.patch.object(d, "get_latest_financials", return_value=fin):
            iv = d.estimate_intrinsic_value(quote)
        self.assertTrue(iv["available"])
        self.assertEqual(iv["model_params"]["raw_eps"], 51.53)
        self.assertAlmostEqual(iv["intrinsic_value"],
                               round(51.53 * 4 / 3, 3) * 25.0 * iv["model_params"]["safety_margin"],
                               places=2)

    def test_valuation_financials_keeps_structure(self):
        fin = self._fetch(notice_date=None).data
        with mock.patch.object(d, "get_stock_quote",
                               return_value={"code": "600519", "price": 1500.0, "pe": 30.0, "pb": 8.0}), \
             mock.patch.object(d, "get_industry_pe",
                               return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney",
                                             "source_label": "实时", "is_static": False}), \
             mock.patch.object(d, "get_latest_financials", return_value=fin), \
             mock.patch.object(d, "get_policy_notes", return_value=[]):
            val = d.get_stock_valuation("600519")
        self.assertEqual(sorted(val["financials"].keys()), sorted([k for k, _, _ in FIELD_MAP]))
        self.assertIsNone(val["financials"]["notice_date"])



class TestGroupI_CachedFinancialsGuard(unittest.TestCase):
    """I：缓存里的财报也要过有效性质检（S3-4A r3）。

    场景覆盖授权文档 §四 的 8 个测试：文件缓存 inf / 内存缓存 nan / 其他字段异常 /
    异常缓存+重抓成功 / 异常缓存+重抓失败 / 合法旧缓存 / 零与 null / 严格 JSON。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.remove(self.tmp.name)
        self._orig_file = d._CACHE_FILE
        d._CACHE_FILE = self.tmp.name
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._IN_FLIGHT.clear()
        self.calls = []

    def tearDown(self):
        d._CACHE_FILE = self._orig_file
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._IN_FLIGHT.clear()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    # ---- 夹具 ----
    def _good_fin(self, **overrides):
        fin = dict(EastMoneyF10Provider().normalize(load_raw(), "600519")[0])
        fin.update(overrides)
        return fin

    def _preload_file(self, fin, ts=None):
        ts = time.time() if ts is None else ts
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"fin_600519": {"__data": fin, "fetched_at": ts}}, f)

    def _preload_memory(self, fin):
        d._IN_MEMORY_CACHE["fin_600519"] = {"data": fin, "time": time.time()}

    def _provider_ok(self):
        def call(name, *args, **kwargs):
            self.calls.append(name)
            return FakeF10(load_raw()).fetch(args[0])
        return call

    def _provider_fail(self):
        def call(name, *args, **kwargs):
            self.calls.append(name)
            return ProviderResult(None, PROVIDER_NAME, "failed", "", 0.0, False,
                                  {"message": "上游不可用"})
        return call

    # ---- 1. 文件缓存 Infinity ----
    def test_file_cache_infinity_rejected(self):
        self._preload_file(self._good_fin(eps=float("inf")))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            self.assertIsNone(d.get_latest_financials("600519"))
        self.assertEqual(self.calls, ["eastmoney_f10"], "坏缓存必须淘汰并尝试重抓一次")

    def test_file_cache_infinity_never_reaches_intrinsic_value(self):
        self._preload_file(self._good_fin(eps=float("inf")))
        quote = {"code": "600519", "price": 1500.0, "pe": 30.0}
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}):
            iv = d.estimate_intrinsic_value(quote)
        self.assertFalse(iv["available"])
        self.assertIsNone(iv["intrinsic_value"])
        json.dumps(iv, allow_nan=False)          # 不得抛 inf/nan 序列化错误

    # ---- 2. 内存缓存 NaN ----
    def test_memory_cache_nan_rejected(self):
        self._preload_memory(self._good_fin(eps=float("nan")))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)):
            self.assertIsNone(d.get_latest_financials("600519"))
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("fin_600519", d._IN_MEMORY_CACHE, "坏内存条目应被淘汰")

    def test_memory_cache_nan_recovered_by_successful_refetch(self):
        self._preload_memory(self._good_fin(eps=float("nan")))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_ok()), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)):
            fin = d.get_latest_financials("600519")
        self.assertIsNotNone(fin)
        self.assertEqual(fin["eps"], 51.53)
        self.assertTrue(math.isfinite(fin["eps"]))

    # ---- 3. 其他财务字段 ----
    def test_other_nonfinite_fields_rejected(self):
        for field_name, value in (("bps", float("inf")), ("revenue", float("nan")),
                                  ("net_profit", float("-inf")), ("roe", float("nan"))):
            self._preload_file(self._good_fin(**{field_name: value}))
            with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
                 mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
                fin = d.get_latest_financials("600519")
            self.assertIsNone(fin, field_name)

    # ---- 4. 异常缓存 + 重抓成功 ----
    def test_invalid_cache_replaced_by_good_data(self):
        self._preload_file(self._good_fin(eps=float("inf")))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_ok()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            first = d.get_latest_financials("600519")
            second = d.get_latest_financials("600519")
        self.assertEqual(first["eps"], 51.53)
        self.assertEqual(sorted(first.keys() & set(k for k, _, _ in FIELD_MAP)),
                         sorted([k for k, _, _ in FIELD_MAP]))
        self.assertEqual(second["eps"], 51.53)
        self.assertEqual(len(self.calls), 1, "恢复后应继续走正常缓存，不再请求上游")
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            disk = json.load(f)["fin_600519"]["__data"]
        self.assertEqual(disk["eps"], 51.53, "坏缓存应被替换为有效财报")

    # ---- 5. 异常缓存 + 重抓失败 ----
    def test_invalid_cache_not_used_as_fallback_when_refetch_fails(self):
        self._preload_file(self._good_fin(eps=float("inf")))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            self.assertIsNone(d.get_latest_financials("600519"))
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            self.assertNotIn("fin_600519", json.load(f), "坏缓存不得留在文件里当兜底")

    def test_invalid_cache_refresh_failure_no_nan_valuation(self):
        self._preload_file(self._good_fin(eps=float("inf")))
        quote = {"code": "600519", "price": 1500.0, "pe": 30.0}
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}):
            iv = d.estimate_intrinsic_value(quote)
        self.assertFalse(iv["available"])
        self.assertIsNone(iv["model_params"]["raw_eps"])
        json.dumps(iv, allow_nan=False)

    # ---- 6. 合法旧缓存 ----
    def test_valid_old_cache_still_served_without_upstream_call(self):
        legacy = {"report_date": "2025-12-31", "report_name": "2025年报", "eps": 1.5,
                  "bps": 10.0, "revenue": 1e9, "revenue_yoy": 3.0, "net_profit": 2e8,
                  "profit_yoy": 4.0, "roe": 12.0, "notice_date": "2026-03-01"}
        self._preload_file(legacy)
        with mock.patch.object(d._providers, "call",
                               side_effect=AssertionError("合法缓存不该请求上游")), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            first = d.get_latest_financials("600519")
            second = d.get_latest_financials("600519")
        self.assertEqual(first["report_date"], "2025-12-31")
        self.assertEqual(first["eps"], 1.5)
        self.assertEqual(second["eps"], 1.5)
        self.assertEqual(self.calls, [])

    # ---- 7. 零与 null ----
    def test_zero_and_null_values_are_valid(self):
        fin = self._good_fin(eps=0, revenue=0, net_profit=0, roe=None,
                             revenue_yoy=None, notice_date=None)
        self._preload_file(fin)
        with mock.patch.object(d._providers, "call",
                               side_effect=AssertionError("合法缓存不该请求上游")), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            got = d.get_latest_financials("600519")
        self.assertEqual(got["eps"], 0)
        self.assertEqual(got["revenue"], 0)
        self.assertEqual(got["net_profit"], 0)
        self.assertIsNone(got["roe"])
        self.assertIsNone(got["notice_date"])
        self.assertTrue(d._fin_values_finite(got))

    # ---- 8. 严格 JSON 序列化 ----
    def test_intrinsic_value_strict_json(self):
        with mock.patch.object(d._providers, "call", side_effect=self._provider_ok()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}):
            quote = {"code": "600519", "price": 1500.0, "pe": 30.0}
            iv = d.estimate_intrinsic_value(quote)
        self.assertTrue(iv["available"])
        blob = json.dumps(iv, allow_nan=False)
        self.assertNotIn("Infinity", blob)
        self.assertNotIn("NaN", blob)

    # ---- 诊断侧通道（S3-4B 预留）----
    def test_diagnostic_records_cache_rejection(self):
        self._preload_file(self._good_fin(eps=float("inf")))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            d.get_latest_financials("600519")
        diag = d.get_financials_diagnostic("600519")
        self.assertEqual(diag["status"], "failed", "重抓也失败：本次结果就是失败")
        self.assertTrue(diag["cache_rejected"], "但要保留缓存曾含非法值这一事实")
        self.assertIn("NaN", diag["reason"])



class TestGroupJ_LayeredCacheBoundaries(unittest.TestCase):
    """J：分层淘汰与有界恢复（S3-4A r4）。

    覆盖授权文档 §四 A~H：内存坏≠文件坏、过期内存坏值不同时触发 SWR+同步重抓、
    多 key 隔离、严格 JSON 与业务回归。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.remove(self.tmp.name)
        self._orig_file = d._CACHE_FILE
        d._CACHE_FILE = self.tmp.name
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._IN_FLIGHT.clear()
        self.calls = []

    def tearDown(self):
        d._CACHE_FILE = self._orig_file
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._IN_FLIGHT.clear()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    # ---- 夹具 ----
    def _good_fin(self, **overrides):
        fin = dict(EastMoneyF10Provider().normalize(load_raw(), "600519")[0])
        fin.update(overrides)
        return fin

    def _write_file_cache(self, payload):
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _read_file_cache(self):
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)

    def _preload_file(self, fin, ts=None):
        ts = time.time() if ts is None else ts
        self._write_file_cache({"fin_600519": {"__data": fin, "fetched_at": ts}})
        return ts

    def _preload_memory(self, fin, age_sec=0):
        d._IN_MEMORY_CACHE["fin_600519"] = {"data": fin, "time": time.time() - age_sec}

    def _provider_ok(self):
        def call(name, *args, **kwargs):
            self.calls.append(name)
            return FakeF10(load_raw()).fetch(args[0])
        return call

    def _provider_fail(self):
        def call(name, *args, **kwargs):
            self.calls.append(name)
            return ProviderResult(None, PROVIDER_NAME, "failed", "", 0.0, False,
                                  {"message": "模拟上游不可用"})
        return call

    # ---- A. 非法内存 + 合法文件 ----
    def test_invalid_memory_does_not_delete_valid_file(self):
        ts = self._preload_file(self._good_fin())             # 文件：合法
        self._preload_memory(self._good_fin(eps=float("inf")))  # 内存：非法
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)):
            fin = d.get_latest_financials("600519")
        self.assertIsNotNone(fin, "合法文件缓存应该被保留并返回")
        self.assertEqual(fin["eps"], 51.53)
        self.assertTrue(all(not isinstance(v, float) or math.isfinite(v) for v in fin.values()))
        self.assertEqual(fin["_asof"], ts, "文件缓存的原始抓取时间不能被刷新")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("fin_600519", d._IN_MEMORY_CACHE, "非法内存应被淘汰")
        self.assertIn("fin_600519", self._read_file_cache(), "合法文件缓存不得被误删")

    # ---- B. 合法内存 + 非法文件 ----
    def test_valid_memory_not_deleted_by_invalid_file(self):
        self._preload_file(self._good_fin(eps=float("nan")))   # 文件：非法
        self._preload_memory(self._good_fin())                 # 内存：合法
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)):
            fin = d.get_latest_financials("600519")
        self.assertEqual(fin["eps"], 51.53)
        self.assertEqual(self.calls, [], "命中合法内存不该请求上游")
        self.assertIn("fin_600519", d._IN_MEMORY_CACHE, "合法内存不得被删除")
        stored = self._read_file_cache()["fin_600519"]["__data"]["eps"]
        self.assertTrue(math.isnan(stored), "没被读到的非法文件在本次调用中不应被改动")

    # ---- C. 两层都非法 ----
    def test_both_layers_invalid(self):
        self._preload_file(self._good_fin(eps=float("nan")))
        self._preload_memory(self._good_fin(eps=float("inf")))
        quote = {"code": "600519", "price": 1500.0, "pe": 30.0}
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}):
            self.assertIsNone(d.get_latest_financials("600519"))
            iv = d.estimate_intrinsic_value(quote)
        self.assertFalse(iv["available"])
        json.dumps(iv, allow_nan=False)
        self.assertNotIn("fin_600519", self._read_file_cache(), "非法文件应被淘汰")
        self.assertNotIn("fin_600519", d._IN_MEMORY_CACHE, "非法内存应被淘汰")
        # 两次业务取数（直接取 + 内在价值内部再取）各一次上游尝试，没有额外重复请求
        self.assertEqual(len(self.calls), 2, "两层都坏时每轮只保留一次上游尝试")

    # ---- D. 过期非法内存：不得 SWR + 同步重抓 ----
    def test_expired_invalid_memory_has_bounded_recovery(self):
        self._preload_memory(self._good_fin(eps=float("inf")), age_sec=7 * 3600)
        spawned = []
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)), \
             mock.patch.object(d, "_spawn_refresh", side_effect=lambda *a, **k: spawned.append(a)):
            fin = d.get_latest_financials("600519")
        self.assertIsNone(fin)
        self.assertEqual(len(self.calls), 1, "一次业务调用最多一次恢复请求")
        self.assertEqual(spawned, [], "非法过期内存不得被当作 SWR 旧数据启动后台刷新")

    def test_expired_invalid_memory_does_not_loop(self):
        self._preload_memory(self._good_fin(eps=float("inf")), age_sec=7 * 3600)
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)):
            for _ in range(3):
                self.assertIsNone(d.get_latest_financials("600519"))
        self.assertEqual(len(self.calls), 3, "每次调用一次，无内部循环放大")

    # ---- E. 合法过期内存：SWR 保持原样 ----
    def test_expired_valid_memory_still_uses_swr(self):
        self._preload_memory(self._good_fin(), age_sec=7 * 3600)
        spawned = []
        with mock.patch.object(d._providers, "call", side_effect=self._provider_ok()), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)), \
             mock.patch.object(d, "_spawn_refresh", side_effect=lambda *a, **k: spawned.append(a)):
            fin = d.get_latest_financials("600519")
        self.assertEqual(fin["eps"], 51.53)
        self.assertEqual(self.calls, [], "SWR 先返回旧数据，不该同步抓取")
        self.assertEqual(len(spawned), 1, "合法过期内存应照旧触发一次后台刷新")
        # 后台刷新的校验函数必须能拒掉坏数据、放行好数据
        validate = spawned[0][2]
        self.assertTrue(validate(self._good_fin()))
        self.assertFalse(validate(self._good_fin(eps=float("inf"))))

    def test_expired_valid_memory_refresh_updates_cache(self):
        self._preload_memory(self._good_fin(eps=1.0), age_sec=7 * 3600)
        captured = {}
        with mock.patch.object(d, "_market_status", return_value=("交易中", True)), \
             mock.patch.object(d, "_spawn_refresh",
                               side_effect=lambda key, fetcher, validate: captured.update(
                                   key=key, fetcher=fetcher, validate=validate)):
            with mock.patch.object(d._providers, "call", side_effect=self._provider_ok()):
                d.get_latest_financials("600519")
                fresh = captured["fetcher"]()          # 模拟后台刷新真正抓取
        self.assertEqual(fresh["eps"], 51.53)
        self.assertTrue(captured["validate"](fresh))

    # ---- F. 刷新成功 / 失败 / 恢复 ----
    def test_invalid_cache_then_failure_then_recovery(self):
        self._preload_file(self._good_fin(eps=float("inf")))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            self.assertIsNone(d.get_latest_financials("600519"))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_ok()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            recovered = d.get_latest_financials("600519")
        self.assertEqual(recovered["eps"], 51.53)
        calls_before = len(self.calls)
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            again = d.get_latest_financials("600519")
        self.assertEqual(again["eps"], 51.53, "恢复后应命中缓存，不再依赖上游")
        self.assertEqual(len(self.calls), calls_before)

    # ---- G. 多 key 隔离 ----
    def test_other_keys_and_stocks_untouched(self):
        good_other = self._good_fin(eps=1.94)
        self._write_file_cache({
            "fin_600519": {"__data": self._good_fin(eps=float("inf")), "fetched_at": time.time()},
            "fin_000001": {"__data": good_other, "fetched_at": time.time()},
            "sector_flow": {"__data": [{"name": "板块A", "flow": 1.0}], "fetched_at": time.time()},
            "__data_meta__": {"fin_000001": {"completeness": "complete"}},
        })
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            self.assertIsNone(d.get_latest_financials("600519"))
            other = d.get_latest_financials("000001")
        disk = self._read_file_cache()
        self.assertNotIn("fin_600519", disk)
        self.assertIn("fin_000001", disk)
        self.assertIn("sector_flow", disk)
        self.assertIn("__data_meta__", disk)
        self.assertEqual(other["eps"], 1.94)
        self.assertEqual(self.calls, ["eastmoney_f10"], "合法股票不该触发上游")

    # ---- H. 严格 JSON 与业务回归 ----
    def test_business_fields_unchanged_after_guard(self):
        fin = self._good_fin()
        self._preload_file(fin)
        with mock.patch.object(d._providers, "call",
                               side_effect=AssertionError("合法缓存不该请求上游")), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            got = d.get_latest_financials("600519")
        self.assertEqual(sorted(got.keys() & set(k for k, _, _ in FIELD_MAP)),
                         sorted([k for k, _, _ in FIELD_MAP]))
        self.assertEqual(got["bps"], 216.949629787912)
        self.assertEqual(got["revenue"], 133283652508.95)
        self.assertEqual(got["notice_date"], "2026-10-31")

    def test_zero_none_and_intrinsic_not_infinite(self):
        for override in ({"eps": 0}, {"eps": None}, {"notice_date": None}):
            self._preload_file(self._good_fin(**override))
            with mock.patch.object(d._providers, "call",
                                   side_effect=AssertionError("合法缓存不该请求上游")), \
                 mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
                got = d.get_latest_financials("600519")
            self.assertIsNotNone(got, str(override))
        quote = {"code": "600519", "price": 1500.0, "pe": 30.0}
        with mock.patch.object(d._providers, "call", side_effect=self._provider_ok()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}):
            iv = d.estimate_intrinsic_value(quote)
        json.dumps(iv, allow_nan=False)
        self.assertTrue(math.isfinite(iv["intrinsic_value"]))



class TestGroupK_ClosedMarketFallback(unittest.TestCase):
    """K：休市时合法内存不能被损坏的文件缓存遮挡（S3-4A r5）。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.remove(self.tmp.name)
        self._orig_file = d._CACHE_FILE
        d._CACHE_FILE = self.tmp.name
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._IN_FLIGHT.clear()
        self.calls = []

    def tearDown(self):
        d._CACHE_FILE = self._orig_file
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._IN_FLIGHT.clear()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def _good_fin(self, **overrides):
        fin = dict(EastMoneyF10Provider().normalize(load_raw(), "600519")[0])
        fin.update(overrides)
        return fin

    def _preload_file(self, fin, ts=None):
        ts = time.time() - 3600 if ts is None else ts
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"fin_600519": {"__data": fin, "fetched_at": ts}}, f)
        return ts

    def _preload_memory(self, fin, age_sec=600):
        ts = time.time() - age_sec
        d._IN_MEMORY_CACHE["fin_600519"] = {"data": fin, "time": ts}
        return ts

    def _provider_ok(self):
        def call(name, *args, **kwargs):
            self.calls.append(name)
            return FakeF10(load_raw()).fetch(args[0])
        return call

    def _provider_fail(self):
        def call(name, *args, **kwargs):
            self.calls.append(name)
            return ProviderResult(None, PROVIDER_NAME, "failed", "", 0.0, False,
                                  {"message": "模拟上游不可达"})
        return call

    # ---- A. 休市：合法内存 + 非法文件 + 上游失败 ----
    def test_closed_market_falls_back_to_valid_memory(self):
        mem_ts = self._preload_memory(self._good_fin())
        self._preload_file(self._good_fin(eps=float("inf")))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            fin = d.get_latest_financials("600519")
        self.assertIsNotNone(fin, "合法内存不该因为文件坏了就一起丢掉")
        self.assertEqual(fin["eps"], 51.53)
        self.assertTrue(math.isfinite(fin["eps"]))
        self.assertEqual(fin["_asof"], mem_ts, "回退必须保留内存原始抓取时间")
        self.assertTrue(fin["_stale"], "回退数据必须标成陈旧，不能假装本次抓取成功")
        self.assertEqual(fin["_fallback"], "memory")
        self.assertIn("eastmoney", fin["_source"])
        self.assertIn("fin_600519", d._IN_MEMORY_CACHE, "合法内存不得被删除")
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            self.assertNotIn("fin_600519", json.load(f), "非法文件应被淘汰")
        self.assertEqual(len(self.calls), 1, "回退不额外请求上游")

    # ---- B. 休市：两层都非法 ----
    def test_closed_market_both_layers_invalid(self):
        self._preload_memory(self._good_fin(eps=float("nan")))
        self._preload_file(self._good_fin(eps=float("inf")))
        quote = {"code": "600519", "price": 1500.0, "pe": 30.0}
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)), \
             mock.patch.object(d, "get_industry_pe", return_value={"pe": 25.0, "industry": "白酒"}):
            self.assertIsNone(d.get_latest_financials("600519"))
            iv = d.estimate_intrinsic_value(quote)
        self.assertFalse(iv["available"])
        json.dumps(iv, allow_nan=False)
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            self.assertNotIn("fin_600519", json.load(f))

    # ---- C. 休市：两层都合法时保持原优先级（文件优先）----
    def test_closed_market_keeps_file_priority_when_both_valid(self):
        file_ts = self._preload_file(self._good_fin(eps=51.53), ts=time.time() - 7200)
        self._preload_memory(self._good_fin(eps=60.0), age_sec=600)
        with mock.patch.object(d._providers, "call",
                               side_effect=AssertionError("休市读盘不该请求上游")), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            fin = d.get_latest_financials("600519")
        self.assertEqual(fin["eps"], 51.53, "正常场景仍按休市策略读文件缓存")
        self.assertEqual(fin["_asof"], file_ts)
        self.assertFalse(fin.get("_fallback") == "memory")

    # ---- D. 交易时段行为不变 ----
    def test_trading_memory_hit_still_priority(self):
        self._preload_memory(self._good_fin())
        self._preload_file(self._good_fin(eps=70.0))
        with mock.patch.object(d._providers, "call",
                               side_effect=AssertionError("命中内存不该请求上游")), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)):
            fin = d.get_latest_financials("600519")
        self.assertEqual(fin["eps"], 51.53, "交易时段仍然内存优先")
        self.assertNotIn("_fallback", fin)

    def test_trading_invalid_memory_valid_file_unchanged(self):
        ts = self._preload_file(self._good_fin())
        self._preload_memory(self._good_fin(eps=float("inf")))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("交易中", True)):
            fin = d.get_latest_financials("600519")
        self.assertEqual(fin["eps"], 51.53)
        self.assertEqual(fin["_asof"], ts)
        self.assertEqual(len(self.calls), 1)

    # ---- E. force=True 语义 ----
    def test_force_refresh_success_returns_fresh(self):
        self._preload_memory(self._good_fin(eps=60.0))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_ok()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            fin = d.get_latest_financials("600519", refresh=True)
        self.assertEqual(fin["eps"], 51.53)
        self.assertNotIn("_fallback", fin)
        self.assertEqual(len(self.calls), 1)

    def test_force_refresh_failure_falls_back_to_memory_once(self):
        mem_ts = self._preload_memory(self._good_fin())
        self._preload_file(self._good_fin(eps=float("nan")))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            fin = d.get_latest_financials("600519", refresh=True)
        self.assertEqual(fin["eps"], 51.53)
        self.assertEqual(fin["_asof"], mem_ts)
        self.assertTrue(fin["_stale"])
        self.assertEqual(len(self.calls), 1, "force 失败也只允许一次上游请求")

    def test_force_refresh_failure_prefers_valid_file(self):
        ts = self._preload_file(self._good_fin(eps=48.0))
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            fin = d.get_latest_financials("600519", refresh=True)
        self.assertEqual(fin["eps"], 48.0, "有合法文件缓存时优先文件（既有语义）")
        self.assertEqual(fin["_asof"], ts)

    # ---- F. 隔离 ----
    def test_fallback_does_not_touch_other_keys(self):
        mem_ts = self._preload_memory(self._good_fin())
        good_other = self._good_fin(eps=1.94)
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "fin_600519": {"__data": self._good_fin(eps=float("inf")), "fetched_at": time.time() - 3600},
                "fin_000001": {"__data": good_other, "fetched_at": time.time() - 3600},
                "sector_flow": {"__data": [{"name": "板块A", "flow": 1.0}], "fetched_at": time.time() - 3600},
                "__data_meta__": {"fin_000001": {"completeness": "complete"}},
            }, f)
        with mock.patch.object(d._providers, "call", side_effect=self._provider_fail()), \
             mock.patch.object(d, "_market_status", return_value=("已收盘", False)):
            fin = d.get_latest_financials("600519")
        self.assertEqual(fin["eps"], 51.53)
        self.assertEqual(fin["_asof"], mem_ts)
        with open(d._CACHE_FILE, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertNotIn("fin_600519", disk)
        self.assertIn("fin_000001", disk)
        self.assertIn("sector_flow", disk)
        self.assertIn("__data_meta__", disk)


if __name__ == "__main__":
    unittest.main(verbosity=2)
