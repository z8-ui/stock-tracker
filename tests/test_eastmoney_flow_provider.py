# -*- coding: utf-8 -*-
"""V2.1 S2：东方财富资金 Provider 的契约与差分测试（纯离线，用真实采集报文）。

锁死四件事：
  1. 迁移前后数值完全一致 —— 本文件里保留一份"迁移前实现"的冻结副本，
     用同一份真实夹具跑，逐键比对（差分测试）。
  2. 三种模式各自的结构：history=逐日 CSV / today=当日 CSV / market_snapshot=diff 对象数组；
     `diff` 绝不能被当成 klines 解析。
  3. 失败语义：空数据、字段不足、坏 JSON、超时、连接异常都必须变成明确状态，
     不许用 0 值或伪造日期顶上。
  4. 上层兼容：get_stock_money_flow / get_stock_real_money_flow / get_market_flow
     的参数与返回结构不变。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import data_service as d                       # noqa: E402
from services.data import registry            # noqa: E402
from services.data.exceptions import (        # noqa: E402
    ProviderEmpty, ProviderFormatError, ProviderNetworkError, ProviderTimeout,
)
from services.data.normalizers import secid, yuan_to_yi   # noqa: E402
from services.data.providers import eastmoney_flow as em   # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "eastmoney")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


DAY_600519 = "fflow_daykline_600519_2026-11-08.json"
DAY_000001 = "fflow_daykline_000001_2026-11-08.json"
DAY_SWAP = "fflow_daykline_swap_600519_2026-11-08.json"
KLINE_600519 = "fflow_kline_600519_2026-11-08.json"
ULIST_INDEX = "ulist_index_2026-11-08.json"
ULIST_NAMED = "ulist_named_600519_2026-11-08.json"


class Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status

    def json(self):
        return json.loads(self.text)


def klines_of(text):
    return json.loads(text)["data"]["klines"]


# --------------------------------------------------------------------------
# 迁移前的实现（冻结副本）：差分测试的参照物，只用于比对，不参与生产调用
# --------------------------------------------------------------------------
def legacy_stock_money_flow(klines, days=20):
    def yi(v):
        try:
            return round(float(v) / 1e8, 2)
        except (TypeError, ValueError):
            return 0.0

    out = {"dates": [], "flows": {"super_large": [], "large": [], "middle": [], "small": []}}
    for row in [k.split(",") for k in klines][-days:]:
        if len(row) < 5:
            continue
        out["dates"].append(row[0][5:] if len(row[0]) >= 10 else row[0])
        out["flows"]["small"].append(yi(row[2]))
        out["flows"]["middle"].append(yi(row[3]))
        out["flows"]["large"].append(yi(row[4]))
        super_large = yi(row[5]) if len(row) > 5 else round(yi(row[1]) - yi(row[4]), 2)
        out["flows"]["super_large"].append(super_large)
    if not out["dates"]:
        return None
    out["unit"] = "亿元"
    out["is_mock"] = False
    return out


def legacy_today_flow(parts, turnover_yi):
    main_force = float(parts[1])
    small_order = float(parts[2])
    middle_order = float(parts[3])
    large_order = float(parts[4])
    super_large = main_force - large_order

    def yi(v):
        return round(v / 1e8, 2)

    def pct(v):
        return round(v / (turnover_yi * 1e8) * 100, 2) if turnover_yi > 0 else 0

    return {
        "main_force": yi(main_force), "super_large": yi(super_large),
        "large": yi(large_order), "middle": yi(middle_order), "small": yi(small_order),
        "main_force_pct": pct(main_force), "super_large_pct": pct(super_large),
        "large_pct": pct(large_order), "middle_pct": pct(middle_order),
        "small_pct": pct(small_order),
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.provider = em.EastMoneyFlowProvider()
        self.tmp = tempfile.TemporaryDirectory()
        self._cache_file = d._CACHE_FILE
        self._status = d._market_status
        self._last = dict(d._LAST_SUCCESS)
        d._CACHE_FILE = os.path.join(self.tmp.name, "cache.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()

    def tearDown(self):
        d._CACHE_FILE = self._cache_file
        d._market_status = self._status
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._LAST_SUCCESS.update(self._last)
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        self.tmp.cleanup()

    def http(self, text=None, side_effect=None):
        return mock.patch.object(requests, "get",
                                 side_effect=side_effect or (lambda *a, **k: Resp(text or "")))


# --------------------------------------------------------------------------
# 1. secid 标准化（设计 §4.5）
# --------------------------------------------------------------------------
class TestSecid(unittest.TestCase):
    def test_stock_codes(self):
        cases = {"600519": "1.600519", "900901": "1.900901", "688981": "1.688981",
                 "000001": "0.000001", "300750": "0.300750", "830799": "0.830799"}
        for code, want in cases.items():
            self.assertEqual(secid(code, "stock"), want, code)

    def test_index_codes(self):
        cases = {"000001": "1.000001", "399001": "0.399001", "399006": "0.399006",
                 "000300": "1.000300", "899050": "0.899050"}
        for code, want in cases.items():
            self.assertEqual(secid(code, "index"), want, code)

    def test_same_code_differs_by_kind(self):
        # 000001 既是上证指数又是平安银行：语义只能由 kind 决定
        self.assertEqual(secid("000001", "index"), "1.000001")
        self.assertEqual(secid("000001", "stock"), "0.000001")
        self.assertNotEqual(secid("000001", "index"), secid("000001", "stock"))

    def test_unknown_code_raises(self):
        for bad in ("700000", "60051", "abc", "", None, "12345a"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                secid(bad, "stock")

    def test_unknown_index_raises(self):
        with self.assertRaises(ValueError):
            secid("123456", "index")

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            secid("600519", "fund")

    def test_yuan_to_yi(self):
        self.assertEqual(yuan_to_yi(-318894480), -3.19)
        self.assertEqual(yuan_to_yi(0), 0.0)          # 真实零值保留
        self.assertEqual(yuan_to_yi(999_999), 0.01)   # 小于 100 万也要换算
        for bad in (None, ""):
            with self.assertRaises(ValueError):
                yuan_to_yi(bad)


# --------------------------------------------------------------------------
# 2. history：与迁移前逐键差分
# --------------------------------------------------------------------------
class TestHistory(Base):
    def run_provider(self, text, days=20, code="600519"):
        with self.http(text):
            return self.provider.fetch(mode="history", code=code, days=days)

    def test_matches_legacy_on_real_600519(self):
        text = fixture(DAY_600519)
        for days in (20, 120, 1):
            res = self.run_provider(text, days=days)
            self.assertEqual(res.status, "success")
            self.assertEqual(res.data, legacy_stock_money_flow(klines_of(text), days),
                             f"days={days} 与迁移前不一致")

    def test_matches_legacy_on_real_000001(self):
        text = fixture(DAY_000001)
        res = self.run_provider(text, days=20, code="000001")
        self.assertEqual(res.data, legacy_stock_money_flow(klines_of(text), 20))

    def test_swap_request_same_numbers(self):
        # 换序请求的响应与首次逐字相同 → 解析结果必须也相同
        a = self.run_provider(fixture(DAY_600519)).data
        b = self.run_provider(fixture(DAY_SWAP)).data
        self.assertEqual(a, b)

    def test_days_slicing_takes_recent(self):
        text = fixture(DAY_600519)
        full = self.run_provider(text, days=120).data["dates"]
        five = self.run_provider(text, days=5).data["dates"]
        self.assertEqual(five, full[-5:])
        self.assertEqual(len(five), 5)

    def test_latest_row_values_and_unit(self):
        res = self.run_provider(fixture(DAY_600519), days=1)
        self.assertEqual(res.data["dates"], ["11-06"])
        self.assertEqual(res.data["unit"], "亿元")
        self.assertFalse(res.data["is_mock"])
        flows = res.data["flows"]
        self.assertEqual(flows["super_large"], [-1.86])
        self.assertEqual(flows["large"], [-1.33])
        self.assertEqual(flows["middle"], [3.19])
        self.assertEqual(flows["small"], [-0.0])
        # asof 来自报文里的日期，不是 now()
        self.assertEqual(res.asof, "2026-11-06")

    def test_short_row_skipped_not_zeroed(self):
        payload = {"data": {"klines": ["2026-11-06,1,2",            # 2 段，跳过
                                       "2026-11-05,100000000,0,0,0,0"]}}
        res = self.run_provider(json.dumps(payload), days=20)
        self.assertEqual(res.data["dates"], ["11-05"])
        self.assertEqual(len(res.data["flows"]["small"]), 1)

    def test_all_short_rows_is_empty_not_zero(self):
        payload = {"data": {"klines": ["2026-11-06,1,2", "2026-11-05,3"]}}
        res = self.run_provider(json.dumps(payload))
        self.assertEqual(res.status, "empty")
        self.assertIsNone(res.data)

    def test_empty_klines(self):
        res = self.run_provider(json.dumps({"data": {"klines": []}}))
        self.assertEqual(res.status, "empty")
        self.assertIsNone(res.data)

    def test_bad_json(self):
        res = self.run_provider("{not json")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderFormatError")

    def test_http_none_body(self):
        with mock.patch.object(requests, "get", return_value=Resp("")):
            res = self.provider.fetch(mode="history", code="600519")
        self.assertEqual(res.status, "empty")
        self.assertEqual(res.error["type"], "ProviderEmpty")

    def test_secid_used_in_request(self):
        seen = {}

        def spy(url, params=None, **kw):
            seen.update(params or {})
            return Resp(fixture(DAY_600519))

        with mock.patch.object(requests, "get", side_effect=spy):
            self.provider.fetch(mode="history", code="000001")
        self.assertEqual(seen["secid"], "0.000001")


# --------------------------------------------------------------------------
# 3. today：当日快照
# --------------------------------------------------------------------------
class TestToday(Base):
    def test_amounts_match_real_capture(self):
        with self.http(fixture(KLINE_600519)):
            res = self.provider.fetch(mode="today", code="600519")
        a = res.data["amounts_yuan"]
        self.assertEqual(a["main_force"], -318894480.0)
        self.assertEqual(a["small"], -108674.0)
        self.assertEqual(a["middle"], 319003136.0)
        self.assertEqual(a["large"], -133286896.0)
        self.assertEqual(a["super_large"], -185607584.0)   # 主力 − 大单
        self.assertEqual(res.data["date"], "2026-11-06")
        self.assertEqual(res.asof, "2026-11-06")

    def test_klines_lmt5_still_single_snapshot(self):
        # kline 是快照接口：lmt=5 也只回 1 行（真实采集已确认）
        with self.http(fixture("fflow_kline_lmt5_600519_2026-11-08.json")):
            res = self.provider.fetch(mode="today", code="600519")
        self.assertEqual(res.data["date"], "2026-11-06")

    def test_short_row_is_format_error(self):
        payload = {"data": {"klines": ["2026-11-06,-318894480.0,-108674.0"]}}
        with self.http(json.dumps(payload)):
            res = self.provider.fetch(mode="today", code="600519")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderFormatError")

    def test_empty_is_empty(self):
        with self.http(json.dumps({"data": {"klines": []}})):
            res = self.provider.fetch(mode="today", code="600519")
        self.assertEqual(res.status, "empty")

    def test_bad_json(self):
        with self.http("<<<"):
            res = self.provider.fetch(mode="today", code="600519")
        self.assertEqual(res.error["type"], "ProviderFormatError")


# --------------------------------------------------------------------------
# 4. market_snapshot：ulist.np 的 diff 是快照数组
# --------------------------------------------------------------------------
class TestMarketSnapshot(Base):
    IDX = ({"name": "上证指数", "code": "000001"}, {"name": "深证成指", "code": "399001"})

    def snap(self, text, indices=None):
        with self.http(text):
            return self.provider.fetch(mode="market_snapshot",
                                       indices=indices or self.IDX)

    def test_real_capture_totals(self):
        res = self.snap(fixture(ULIST_INDEX))
        self.assertEqual(res.status, "success")
        total = res.data["total"]
        self.assertEqual(total["main_force"], -524.97)
        self.assertEqual(total["super_large"], -233.21)
        self.assertEqual(total["large_order"], -291.77)
        self.assertEqual(total["middle_order"], 45.83)
        self.assertEqual(total["small_order"], 479.15)
        self.assertFalse(res.data["partial"])

    def test_per_index_rows(self):
        res = self.snap(fixture(ULIST_INDEX))
        self.assertEqual([r["name"] for r in res.data["indices"]], ["上证指数", "深证成指"])
        first = res.data["indices"][0]
        self.assertEqual(first["main_force"], -202.34)
        self.assertEqual(first["super_large"], -90.11)
        self.assertEqual(first["large_order"], -112.23)
        self.assertEqual(first["middle_order"], 36.71)
        self.assertEqual(first["small_order"], 165.63)

    def test_secids_from_index_semantics(self):
        seen = {}

        def spy(url, params=None, **kw):
            seen.update(params or {})
            return Resp(fixture(ULIST_INDEX))

        with mock.patch.object(requests, "get", side_effect=spy):
            self.provider.fetch(mode="market_snapshot", indices=self.IDX)
        self.assertEqual(seen["secids"], "1.000001,0.399001")

    def test_asof_is_empty_no_fake_date(self):
        # ulist.np 没有任何时间戳 → 不许用 now() 造一个
        res = self.snap(fixture(ULIST_INDEX))
        self.assertEqual(res.asof, "")

    def test_missing_middle_small_is_none_not_zero(self):
        payload = {"data": {"diff": [{"f62": 1e8, "f66": 5e7, "f72": 5e7}]}}
        res = self.snap(json.dumps(payload))
        self.assertEqual(res.status, "success")
        self.assertTrue(res.data["partial"])
        self.assertIsNone(res.data["total"]["middle_order"])
        self.assertIsNone(res.data["total"]["small_order"])
        self.assertIsNone(res.data["indices"][0]["middle_order"])
        self.assertEqual(res.data["total"]["main_force"], 1.0)

    def test_missing_required_field_fails(self):
        payload = {"data": {"diff": [{"f62": 1e8, "f66": 5e7}]}}   # 缺 f72
        res = self.snap(json.dumps(payload))
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderFormatError")
        self.assertIsNone(res.data)

    def test_empty_diff(self):
        res = self.snap(json.dumps({"data": {"diff": []}}))
        self.assertEqual(res.status, "empty")
        self.assertIsNone(res.data)

    def test_percent_fields_never_used_as_money(self):
        # f69/f75/f81/f87 是占比：若被当金额，主力会变成 -117/-145 这种数量级
        res = self.snap(fixture(ULIST_INDEX))
        self.assertGreater(abs(res.data["total"]["main_force"]), 100)
        self.assertEqual(res.data["total"]["main_force"], -524.97)

    def test_real_zero_kept(self):
        payload = {"data": {"diff": [{"f62": 0, "f66": 0, "f72": 0, "f78": 0, "f84": 0}]}}
        res = self.snap(json.dumps(payload))
        self.assertEqual(res.status, "success")
        self.assertEqual(res.data["total"]["main_force"], 0.0)
        self.assertFalse(res.data["partial"])

    def test_no_indices_argument(self):
        with self.http(fixture(ULIST_INDEX)):
            res = self.provider.fetch(mode="market_snapshot", indices=[])
        self.assertEqual(res.status, "failed")

    def test_bad_json(self):
        res = self.snap("{oops")
        self.assertEqual(res.error["type"], "ProviderFormatError")


# --------------------------------------------------------------------------
# 5. 失败语义：超时 / 网络异常 / 未知模式
# --------------------------------------------------------------------------
class TestFailures(Base):
    def test_timeout(self):
        with mock.patch.object(requests, "get", side_effect=requests.Timeout("slow")):
            res = self.provider.fetch(mode="history", code="600519")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderTimeout")

    def test_network_error(self):
        with mock.patch.object(requests, "get", side_effect=requests.ConnectionError("down")):
            res = self.provider.fetch(mode="today", code="600519")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error["type"], "ProviderNetworkError")

    def test_unknown_mode(self):
        res = self.provider.fetch(mode="nope", code="600519")
        self.assertEqual(res.status, "failed")

    def test_bad_code_does_not_raise(self):
        res = self.provider.fetch(mode="history", code="700000")
        self.assertEqual(res.status, "failed")      # ValueError 被 fetch 收成状态，不冒到路由


# --------------------------------------------------------------------------
# 6. 结构隔离：diff 不能被当 klines，klines 不能被当 diff
# --------------------------------------------------------------------------
class TestStructureIsolation(Base):
    def test_ulist_into_history_is_not_success(self):
        with self.http(fixture(ULIST_INDEX)):
            res = self.provider.fetch(mode="history", code="600519")
        self.assertIn(res.status, ("empty", "failed"))
        self.assertIsNone(res.data)

    def test_klines_into_snapshot_is_not_success(self):
        with self.http(fixture(KLINE_600519)):
            res = self.provider.fetch(mode="market_snapshot", indices=TestMarketSnapshot.IDX)
        self.assertIn(res.status, ("empty", "failed"))


# --------------------------------------------------------------------------
# 7. 上层兼容：三个函数对外结构不变
# --------------------------------------------------------------------------
class TestDataServiceCompat(Base):
    def test_get_stock_money_flow_matches_legacy(self):
        text = fixture(DAY_600519)
        with self.http(text):
            got = d.get_stock_money_flow("600519", days=20)
        # 缓存层会额外注入 _asof/_market_status/_source 这类元数据，业务字段逐键比对
        payload = {k: v for k, v in got.items() if not k.startswith("_")}
        self.assertEqual(payload, legacy_stock_money_flow(klines_of(text), 20))
        self.assertIn("_asof", got)          # 元数据仍由缓存层提供，语义未变
        self.assertEqual(set(payload), {"dates", "flows", "unit", "is_mock"})
        self.assertEqual(set(payload["flows"]), {"super_large", "large", "middle", "small"})

    def test_get_stock_money_flow_none_on_failure(self):
        with mock.patch.object(requests, "get", side_effect=requests.Timeout("x")):
            self.assertIsNone(d.get_stock_money_flow("600519"))

    def test_get_stock_real_money_flow_matches_legacy(self):
        quote = {"turnover": 25.0}
        text = fixture(KLINE_600519)
        with mock.patch.object(d, "get_stock_quote", return_value=quote):
            with self.http(text):
                got = d.get_stock_real_money_flow("600519")
        parts = klines_of(text)[0].split(",")
        self.assertEqual(got, legacy_today_flow(parts, 25.0))
        self.assertEqual(len(got), 10)

    def test_get_stock_real_money_flow_none_without_quote(self):
        with mock.patch.object(d, "get_stock_quote", return_value=None):
            self.assertIsNone(d.get_stock_real_money_flow("600519"))

    def test_get_market_flow_structure_and_values(self):
        d._market_status = lambda: ("交易中", True)
        parts = [""] * 60
        parts[1], parts[3], parts[30], parts[32] = "上证指数", "3649.18", "20261106150000", "-1.1"
        tencent = "~".join(parts)

        def router(url, params=None, **kw):
            if "gtimg" in url:
                return Resp(tencent)
            return Resp(fixture(ULIST_INDEX))

        with mock.patch.object(requests, "get", side_effect=router):
            out = d.get_market_flow()

        self.assertIsNotNone(out)
        self.assertEqual(out["_flow_fields_v"], 2)
        self.assertTrue(out["_flow_available"])
        self.assertFalse(out["_flow_partial"])
        self.assertEqual(out["main_force"], -524.97)
        self.assertEqual(out["super_large"], -233.21)
        self.assertEqual(out["large_order"], -291.77)
        self.assertEqual(out["middle_order"], 45.83)
        self.assertEqual(out["small_order"], 479.15)
        self.assertEqual(out["date"], "20261106")
        self.assertEqual(out["index_price"], 3649.18)
        for key in ("main_force", "super_large", "large_order", "middle_order",
                    "small_order", "date", "index_name", "index_price", "index_change"):
            self.assertIn(key, out)

    def test_get_market_flow_none_when_flow_fails(self):
        d._market_status = lambda: ("交易中", True)
        parts = [""] * 60
        parts[1], parts[3], parts[30], parts[32] = "上证指数", "3649.18", "20261106150000", "-1.1"

        def router(url, params=None, **kw):
            if "gtimg" in url:
                return Resp("~".join(parts))
            raise requests.Timeout("em down")

        with mock.patch.object(requests, "get", side_effect=router):
            self.assertIsNone(d.get_market_flow())    # 不许组装成 0.00 亿


if __name__ == "__main__":
    unittest.main()
