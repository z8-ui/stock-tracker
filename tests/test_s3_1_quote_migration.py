# -*- coding: utf-8 -*-
"""S3-1 验收测试：腾讯行情解析迁入 Provider + 盘中 30 秒缓存。

覆盖 GPT 评审给出的 11 条条件：
  1 真实 S3-0 夹具          → TestFixtureEquivalence
  2 迁移前后 18 字段对照     → TestMigrationDiff
  3 沪/深代码转换            → TestSecurityCodeConversion
  4 30 秒内 5 次请求=1 次上游 → TestQuoteCache
  5 不同证券缓存键隔离        → TestQuoteCache
  6 TTL 过期触发重建          → TestQuoteCache
  7 命中不刷新 fetched_at     → TestQuoteCache
  8 休市用文件缓存、不冒充实时 → TestQuoteCache
  9 网络失败/缓存降级/无数据   → TestQuoteCache
 10 原有 563 项保留           → 全量回归日志（本文件不减少任何旧用例）
 11 前端 API 契约不变         → TestApiContract

全部离线：网络用 mock，缓存文件写临时目录。
"""

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import requests as real_requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_service as d                                    # noqa: E402
from services.data.exceptions import ProviderFormatError     # noqa: E402
from services.data.providers import tencent as tp           # noqa: E402
from services.data.providers.tencent import TencentQuoteProvider  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "s3_0")
FIXTURE_KEY = "tencent_quote_sh600519"


def fixture_text():
    """S3-0 采集的真实腾讯快照（GBK 原文），只读，不修改。"""
    with open(os.path.join(FIX, FIXTURE_KEY + ".raw"), "rb") as f:
        return f.read().decode("gbk")


def snapshot(n=88, **overrides):
    """按腾讯 ~ 协议拼一份快照；下标语义见 provider.FIELD_INDEX 注释。"""
    vals = [""] * n
    base = {
        0: "1", 1: "贵州茅台", 2: "600519", 3: "1453.00", 4: "1440.00", 5: "1445.00",
        6: "43845.000", 30: "20261110150000", 31: "13.00", 32: "0.90",
        33: "1460.00", 34: "1438.00", 38: "0.35", 39: "22.11", 43: "1.52",
        44: "18260.00", 46: "7.80", 47: "1800.00", 48: "1200.00", 57: "658000.00",
    }
    for k, v in base.items():
        if k < n:
            vals[k] = v
    for k, v in overrides.items():
        if int(k) < n:
            vals[int(k)] = v
    return 'v_sh600519="' + "~".join(vals) + '";'


class Resp:
    def __init__(self, text, encoding="gbk"):
        self.text = text
        self.encoding = encoding
        self.status_code = 200


# --------------------------------------------------------------- 迁移前实现
# 逐行照抄 S3-1 之前的 get_stock_quote 解析段，唯一用途是"迁移前后逐字段对照"。
def old_parse(parts, stock_code):
    try:
        name = parts[1]
        price = float(parts[3]) if parts[3] else 0
        pre_close = float(parts[4]) if parts[4] else 0
        open_price = float(parts[5]) if parts[5] else 0
        high = float(parts[33]) if len(parts) > 33 and parts[33] else 0
        low = float(parts[34]) if len(parts) > 34 and parts[34] else 0
        change = float(parts[31]) if parts[31] else 0
        change_pct = float(parts[32]) if parts[32] else 0
        volume_lots = int(float(parts[6])) if parts[6] else 0
        pe = float(parts[39]) if len(parts) > 39 and parts[39] else 0
        pb = float(parts[46]) if len(parts) > 46 and parts[46] else 0
        turnover_rate = float(parts[38]) if len(parts) > 38 and parts[38] else 0
        market_cap = float(parts[44]) if len(parts) > 44 and parts[44] else 0
        turnover = float(parts[57]) if len(parts) > 57 and parts[57] else 0
        amplitude = float(parts[43]) if len(parts) > 43 and parts[43] else 0
        high_52w = float(parts[47]) if len(parts) > 47 and parts[47] else 0
        low_52w = float(parts[48]) if len(parts) > 48 and parts[48] else 0
        if price <= 0:
            return None
        return {
            "name": name, "code": stock_code,
            "price": price, "pre_close": pre_close, "open": open_price,
            "high": high, "low": low,
            "change": change, "change_pct": change_pct,
            "volume_lots": volume_lots,
            "turnover": round(turnover / 10000, 2),
            "turnover_rate": turnover_rate, "amplitude": amplitude,
            "pe": pe, "pb": pb, "market_cap": market_cap,
            "high_52w": high_52w, "low_52w": low_52w,
        }
    except Exception:
        return None


class QuoteCacheCase(unittest.TestCase):
    """所有用例：临时缓存文件 + 空内存缓存 + 默认盘中，互不污染。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache_file = d._CACHE_FILE
        self._status = d._market_status
        d._CACHE_FILE = os.path.join(self.tmp.name, "cache.json")
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._QUOTE_ATTEMPT.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        d._market_status = lambda: ("交易中", True)
        self.calls = 0

    def tearDown(self):
        d._CACHE_FILE = self._cache_file
        d._market_status = self._status
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._QUOTE_ATTEMPT.clear()
        with d._CACHE_LOCK:
            d._IN_FLIGHT.clear()
        self.tmp.cleanup()

    # --- 工具：让腾讯请求可控并计数 ---
    def serve(self, text=None, exc=None):
        text = snapshot() if text is None else text

        def fake_get(url, **kw):
            self.calls += 1
            if exc is not None:
                raise exc
            self.assertIn("qt.gtimg.cn/q=", url)
            return Resp(text)

        return mock.patch.object(tp.requests, "get", side_effect=fake_get)

    def seed_file_cache(self, code="600519", age_sec=0, price=1453.00):
        """写一份"上一次收盘"的文件缓存，模拟进程重启后读盘。"""
        payload = old_parse(snapshot(**{"3": f"{price:.2f}"}).split("~"), code)
        d._cache_update(f"quote_{code}", payload, time.time() - age_sec)
        return payload


# ---------------------------------------------------------------- 1 真实夹具
class TestFixtureEquivalence(QuoteCacheCase):
    def test_real_s3_0_fixture_parses_to_valid_quote(self):
        text = fixture_text()
        parts = TencentQuoteProvider().normalize(text, code="600519")[0]
        q = TencentQuoteProvider().parse_quote(parts, "600519")
        self.assertEqual(q["name"], "贵州茅台")
        self.assertEqual(q["code"], "600519")
        self.assertGreater(q["price"], 0, "真实报文必须有有效价格")
        self.assertEqual(len(q), 18)
        # 字段来源可回溯到 fixture 的下标，不是重新算出来的
        self.assertEqual(q["price"], float(parts[3]))
        self.assertEqual(q["volume_lots"], int(float(parts[6])))

    def test_real_fixture_through_public_function(self):
        with self.serve(fixture_text()):
            q = d.get_stock_quote("600519")
        expected = old_parse(TencentQuoteProvider().normalize(fixture_text(), "600519")[0], "600519")
        self.assertEqual(q, expected, "真实夹具上，新实现必须与迁移前逐字段一致")

    def test_fixture_file_is_untouched(self):
        """S3-1 不允许改原始 fixture。"""
        with open(os.path.join(FIX, FIXTURE_KEY + ".meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        import hashlib
        with open(os.path.join(FIX, FIXTURE_KEY + ".raw"), "rb") as f:
            raw = f.read()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["sha256"])
        self.assertEqual(len(raw), meta["bytes"])


# ------------------------------------------------------------ 2 迁移前后对照
class TestMigrationDiff(QuoteCacheCase):
    def test_eighteen_fields_identical_across_cases(self):
        cases = {
            "真实 88 字段": fixture_text(),
            "合成 88 字段": snapshot(),
            "刚好到解析下限（58 数据字段）": snapshot(n=59),
            "成交量小数（43845.000）": snapshot(**{"6": "43845.000"}),
            "成交量为真实 0": snapshot(**{"6": "0"}),
            "涨跌为负": snapshot(**{"31": "-12.30", "32": "-0.84"}),
        }
        # 注：字段为空（""）的用例不放在这里 —— 本轮把"空"改为 None（旧实现是 0.0），
        # 差异由 TestNullSemantics 显式对照，不再混进"等价性"断言。
        provider = TencentQuoteProvider()
        for label, text in cases.items():
            with self.subTest(case=label):
                parts = provider.normalize(text, code="600519")[0]
                old = old_parse(parts, "600519")
                if old is None:                      # 价格无效：解析层不管业务，上层才拒
                    self.assertLessEqual(provider.parse_quote(parts, "600519")["price"], 0)
                    continue
                self.assertEqual(provider.parse_quote(parts, "600519"), old,
                                 f"{label}: 18 个字段必须逐项相同")

    def test_field_set_is_exactly_eighteen(self):
        q = TencentQuoteProvider().parse_quote(snapshot().split("~"), "600519")
        self.assertEqual(len(q), 18)
        self.assertEqual(set(q), set(TencentQuoteProvider.QUOTE_FIELDS))

    def test_bad_number_is_reported_not_silently_zero(self):
        parts = snapshot().split("~")
        parts[46] = "-"                              # PB 给了占位符而不是数字
        with self.assertRaises(Exception) as ctx:
            TencentQuoteProvider().parse_quote(parts, "600519")
        self.assertIn("pb", str(ctx.exception))

    def test_public_function_returns_none_on_bad_number(self):
        with self.serve(snapshot(**{"46": "n/a"})):
            self.assertIsNone(d.get_stock_quote("600519"))


# -------------------------------------------------------- 3 沪/深代码转换
class TestSecurityCodeConversion(QuoteCacheCase):
    def test_shanghai_prefix(self):
        p = TencentQuoteProvider()
        self.assertEqual(p.build_url("600519"), "http://qt.gtimg.cn/q=sh600519")
        self.assertEqual(p.build_url("900901"), "http://qt.gtimg.cn/q=sh900901")  # 沪 B

    def test_shenzhen_prefix(self):
        p = TencentQuoteProvider()
        self.assertEqual(p.build_url("000858"), "http://qt.gtimg.cn/q=sz000858")
        self.assertEqual(p.build_url("300750"), "http://qt.gtimg.cn/q=sz300750")  # 创业板

    def test_index_semantics_kept(self):
        # 历史行为：000001 是上证指数（sh），不是平安银行 —— 迁移不得改变
        p = TencentQuoteProvider()
        self.assertEqual(p.build_url("000001"), "http://qt.gtimg.cn/q=sh000001")

    def test_code_is_preserved_in_output(self):
        p = TencentQuoteProvider()
        for code in ("600519", "000858", "300750"):
            self.assertEqual(p.parse_quote(snapshot().split("~"), code)["code"], code)

    def test_sh_and_sz_hit_different_upstream_and_cache_keys(self):
        with self.serve():
            d.get_stock_quote("600519")
            d.get_stock_quote("000858")
        self.assertEqual(self.calls, 2, "两只证券各抓一次，不能共用缓存")
        self.assertEqual(d.quote_cache_meta("600519")["status"], "live")
        self.assertEqual(d.quote_cache_meta("000858")["status"], "live")


# ------------------------------------------------------------- 4-9 缓存行为
class TestQuoteCache(QuoteCacheCase):
    def test_five_calls_in_30s_hit_upstream_once(self):
        with self.serve():
            for _ in range(5):
                self.assertIsNotNone(d.get_stock_quote("600519"))
        self.assertEqual(self.calls, 1, "30 秒内同一证券 5 次请求只能打 1 次上游")

    def test_cache_hit_does_not_refresh_fetched_at(self):
        with self.serve():
            d.get_stock_quote("600519")
            ts1 = d._LAST_SUCCESS["quote_600519"]
            first_asof = d.quote_cache_meta("600519")["asof"]
            import time as _t
            _t.sleep(0.05)
            d.get_stock_quote("600519")
        self.assertEqual(d._LAST_SUCCESS["quote_600519"], ts1, "命中缓存不得刷新 fetched_at")
        self.assertEqual(d.quote_cache_meta("600519")["asof"], first_asof)

    def test_different_codes_isolated(self):
        with self.serve():
            d.get_stock_quote("600519")
            self.assertEqual(self.calls, 1)
            d.get_stock_quote("000858")           # 另一只证券必须重新抓
            self.assertEqual(self.calls, 2)
            d.get_stock_quote("600519")           # 回到第一只仍用缓存
            self.assertEqual(self.calls, 2)

    def test_ttl_expiry_triggers_rebuild(self):
        with self.serve():
            d.get_stock_quote("600519")
            self.assertEqual(self.calls, 1)
            # 把内存时间戳推到 31 秒前 → 过期
            d._IN_MEMORY_CACHE["quote_600519"]["time"] -= 31
            spawned = []
            with mock.patch.object(d, "_spawn_refresh",
                                   side_effect=lambda key, fn, vf=None: spawned.append(key)):
                q = d.get_stock_quote("600519")   # SWR：先给旧值，后台重建
        self.assertIsNotNone(q)
        self.assertEqual(spawned, ["quote_600519"], "TTL 过期必须触发后台重建")
        self.assertEqual(self.calls, 1, "过期这一次仍走 SWR，不等后台完成")

    def test_closed_market_uses_file_cache_without_upstream(self):
        self.seed_file_cache(age_sec=3600)
        d._IN_MEMORY_CACHE.clear()
        d._market_status = lambda: ("已收盘", False)
        with self.serve():
            q = d.get_stock_quote("600519")
        self.assertEqual(self.calls, 0, "休市必须走文件缓存，不打上游")
        self.assertIsNotNone(q)
        self.assertEqual(len(q), 18)
        meta = d.quote_cache_meta("600519")
        self.assertEqual(meta["market_status"], "已收盘")
        self.assertEqual(meta["status"], "cached", "休市快照不能被标成实时")
        self.assertGreater(meta["age_sec"], 3000)

    def test_network_failure_degrades_to_cache_but_says_so(self):
        self.seed_file_cache(age_sec=600)
        with self.serve(exc=real_requests.Timeout("t")):
            q = d.get_stock_quote("600519")
        self.assertIsNotNone(q, "有旧缓存时不能直接 500")
        meta = d.quote_cache_meta("600519")
        self.assertEqual(meta["status"], "cached")
        self.assertTrue(meta["is_stale"] or meta["age_sec"] >= 600)
        self.assertIn("上游无数据", meta["reason"])

    def test_failure_without_cache_is_unavailable_not_zero_quote(self):
        with self.serve(exc=real_requests.Timeout("t")):
            q = d.get_stock_quote("600519")
        self.assertIsNone(q, "无数据必须 None，不能返回全零行情")
        meta = d.quote_cache_meta("600519")
        self.assertEqual(meta["status"], "unavailable")
        self.assertIn("上游无数据", meta["reason"])

    def test_three_states_are_distinguishable(self):
        with self.serve():
            d.get_stock_quote("600519")
        live = d.quote_cache_meta("600519")["status"]
        self.assertEqual(live, "live")

        # 清内存、留文件缓存 → 上游失败时必须降级而不是报"实时"
        d._IN_MEMORY_CACHE.clear()
        d._LAST_SUCCESS.clear()
        d._QUOTE_ATTEMPT.clear()
        self.seed_file_cache(age_sec=60)
        with self.serve(exc=real_requests.Timeout("t")):
            d.get_stock_quote("600519")
        degraded = d.quote_cache_meta("600519")["status"]
        self.assertEqual(degraded, "cached")

        # 换一只没缓存的证券 + 上游失败 → 无数据
        d._CACHE_FILE = os.path.join(self.tmp.name, "empty.json")
        with self.serve(exc=real_requests.Timeout("t")):
            d.get_stock_quote("000858")
        unavailable = d.quote_cache_meta("000858")["status"]
        self.assertEqual(unavailable, "unavailable")

        self.assertEqual(len({live, degraded, unavailable}), 3,
                         "实时 / 缓存降级 / 无数据 三种状态必须能区分开")

    def test_zero_price_is_not_cached(self):
        with self.serve(snapshot(**{"3": "0"})):
            self.assertIsNone(d.get_stock_quote("600519"))
        self.assertNotIn("quote_600519", d._IN_MEMORY_CACHE,
                         "无效行情不得进缓存，否则 30 秒内会一直返回 0 元")
        self.assertEqual(d.quote_cache_meta("600519")["status"], "unavailable")


# ------------------------------------------------- 空值 ≠ 真实零值（本轮必改 1）
class TestNullSemantics(QuoteCacheCase):
    """上游没给值 → null；上游给了 "0" → 真实 0。两者不能混。"""

    def _quote(self, text):
        parts = TencentQuoteProvider().normalize(text, code="600519")[0]
        return TencentQuoteProvider().parse_quote(parts, "600519")

    def test_empty_pe_is_null_not_zero(self):
        q = self._quote(snapshot(**{"39": ""}))
        self.assertIsNone(q["pe"])
        self.assertNotEqual(q["pe"], 0)

    def test_empty_pb_is_null_not_zero(self):
        q = self._quote(snapshot(**{"46": ""}))
        self.assertIsNone(q["pb"])

    def test_empty_52w_bounds_are_null(self):
        q = self._quote(snapshot(**{"47": "", "48": ""}))
        self.assertIsNone(q["high_52w"])
        self.assertIsNone(q["low_52w"])

    def test_empty_turnover_is_null(self):
        q = self._quote(snapshot(**{"57": ""}))
        self.assertIsNone(q["turnover"])

    def test_empty_volume_is_null(self):
        q = self._quote(snapshot(**{"6": ""}))
        self.assertIsNone(q["volume_lots"])

    def test_string_zero_is_a_real_zero(self):
        q = self._quote(snapshot(**{"39": "0", "46": "0", "57": "0", "6": "0"}))
        for f in ("pe", "pb", "turnover"):
            self.assertEqual(q[f], 0.0, f"{f} 的 \"0\" 必须是真实 0，不是 None")
        self.assertEqual(q["volume_lots"], 0)

    def test_old_implementation_turned_empty_into_zero(self):
        """把差异写清楚：旧实现空字段→0.0，新实现→None。这是本轮有意的语义修正。"""
        parts = snapshot(**{"39": "", "46": "", "57": "", "6": ""}).split("~")
        old = old_parse(parts, "600519")
        new = self._quote(snapshot(**{"39": "", "46": "", "57": "", "6": ""}))
        for f in ("pe", "pb", "turnover", "volume_lots"):
            self.assertEqual(old[f], 0, f"旧实现把 {f} 补成了 0")
            self.assertIsNone(new[f], f"新实现必须保留 null")

    def test_empty_price_is_structural_failure(self):
        """必需字段为空 = 这份报文没有行情，不是"价格未知"。"""
        with self.assertRaises(ProviderFormatError):
            self._quote(snapshot(**{"3": ""}))
        with self.serve(snapshot(**{"3": ""})):
            self.assertIsNone(d.get_stock_quote("600519"))

    def test_public_function_keeps_null_and_json_shows_null(self):
        with self.serve(snapshot(**{"39": "", "46": "", "57": ""})):
            q = d.get_stock_quote("600519")
        self.assertIsNone(q["pe"])
        self.assertIsNone(q["pb"])
        self.assertIsNone(q["turnover"])
        payload = json.loads(json.dumps(q))
        self.assertIsNone(payload["pe"], "JSON 里必须是 null，不能变成 0")
        self.assertEqual(len(q), 18, "键结构不变，只是值可以是 null")

    def test_valuation_handles_null_pe_and_keeps_null_pb(self):
        quote = {"code": "600519", "price": 100.0, "pe": None, "pb": None}
        with mock.patch.object(d, "get_stock_quote", return_value=quote), \
             mock.patch.object(d, "get_industry_pe",
                               return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney"}), \
             mock.patch.object(d, "get_latest_financials", return_value=None), \
             mock.patch.object(d, "get_policy_notes", return_value=[]):
            v = d.get_stock_valuation("600519")
        self.assertIsNone(v["pe"])
        self.assertIsNone(v["pb"], "pb 缺失保持 null，不再补 0")
        self.assertFalse(v["valuation_available"])
        self.assertEqual(v["level"], "数据不可用")

    def test_intrinsic_value_handles_null_pe(self):
        with mock.patch.object(d, "get_industry_pe",
                               return_value={"pe": 25.0, "industry": "白酒", "source": "eastmoney"}), \
             mock.patch.object(d, "get_latest_financials",
                               return_value={"eps": 1.2, "report_date": "2026-09-30"}):
            iv = d.estimate_intrinsic_value({"code": "600519", "price": 100.0, "pe": None})
        self.assertIsNone(iv["pe"])
        self.assertIsNone(iv["pe_reference"], "PE 为 null 时不得算出相对 PE 参考值")

    def test_real_money_flow_without_turnover_has_null_pct(self):
        amounts = {"main_force": 1e8, "super_large": 5e7, "large": 5e7, "middle": 0, "small": 0}
        fake = SimpleNamespace(ok=lambda: True, data={"amounts_yuan": amounts})
        quote = {"code": "600519", "price": 1439.0, "turnover": None}
        with mock.patch.object(d, "get_stock_quote", return_value=quote), \
             mock.patch.object(d._providers, "call", return_value=fake):
            out = d.get_stock_real_money_flow("600519")
        self.assertEqual(out["main_force"], 1.0, "金额本身照常给")
        for f in ("main_force_pct", "super_large_pct", "large_pct", "middle_pct", "small_pct"):
            self.assertIsNone(out[f], f"{f}：没有分母就必须是 null，不是 0%，也不是 0.01 亿算出的假比例")

    def test_real_money_flow_zero_turnover_is_null_not_fake_ratio(self):
        amounts = {"main_force": 1e8, "super_large": 0, "large": 0, "middle": 0, "small": 0}
        fake = SimpleNamespace(ok=lambda: True, data={"amounts_yuan": amounts})
        quote = {"code": "600519", "price": 1439.0, "turnover": 0.0}
        with mock.patch.object(d, "get_stock_quote", return_value=quote), \
             mock.patch.object(d._providers, "call", return_value=fake):
            out = d.get_stock_real_money_flow("600519")
        self.assertIsNone(out["main_force_pct"],
                          "停牌 0 成交额不得被 max(...,0.01) 放大成有效百分比")


# --------------------------- 无数据时不得靠历史成功时间标成 cached（必改 2）
class TestNoDataWithHistory(QuoteCacheCase):
    def test_history_success_does_not_make_current_call_cached(self):
        # 1) 先成功一次，留下历史 _LAST_SUCCESS
        with self.serve():
            d.get_stock_quote("600519")
        self.assertEqual(d.quote_cache_meta("600519")["status"], "live")
        self.assertIn("quote_600519", d._LAST_SUCCESS)

        # 2) 清掉内存缓存 + 文件缓存（模拟缓存被清空/损坏），保留历史成功时间
        d._IN_MEMORY_CACHE.clear()
        d._CACHE_FILE = os.path.join(self.tmp.name, "corrupted.json")
        with open(d._CACHE_FILE, "w", encoding="utf-8") as f:
            f.write("{ this is not json")          # 损坏的缓存文件
        self.assertIn("quote_600519", d._LAST_SUCCESS, "历史成功时间仍然在")

        # 3) 下一次上游失败
        with self.serve(exc=real_requests.Timeout("t")):
            self.assertIsNone(d.get_stock_quote("600519"))

        meta = d.quote_cache_meta("600519")
        self.assertEqual(meta["served_from"], "none")
        self.assertEqual(meta["last_fetch_status"], "failed")
        self.assertEqual(meta["status"], "unavailable",
                         "历史 asof 存在也不能把当前无数据说成 cached")

    def test_normal_cache_paths_still_cached(self):
        """修复不能误伤正常路径：内存/文件/降级三种来源仍标记为 cached。"""
        with self.serve():
            d.get_stock_quote("600519")
            d.get_stock_quote("600519")
        self.assertEqual(d.quote_cache_meta("600519")["status"], "cached")   # memory

        d._IN_MEMORY_CACHE.clear()
        d._market_status = lambda: ("已收盘", False)
        with self.serve():
            d.get_stock_quote("600519")
        self.assertEqual(d.quote_cache_meta("600519")["status"], "cached")   # file

        d._IN_MEMORY_CACHE.clear()
        d._market_status = lambda: ("交易中", True)
        with self.serve(exc=real_requests.Timeout("t")):
            d.get_stock_quote("600519")
        self.assertEqual(d.quote_cache_meta("600519")["status"], "cached")   # file_fallback


# --------------------- 并发 single-flight：来源由 _cached_get 记录（必改 3）
class TestConcurrentSingleFlight(QuoteCacheCase):
    def test_waiter_is_memory_not_file(self):
        import threading as _t

        entered = _t.Event()      # A 已进入上游请求
        release = _t.Event()      # 允许 A 返回
        b_waiting = _t.Event()    # B 已进入 single-flight 等待
        results = {}
        served_log = []

        text = snapshot()

        def fake_get(url, **kw):
            self.calls += 1
            entered.set()
            self.assertTrue(release.wait(5), "A 的上游请求必须被放行")
            return Resp(text)

        # 记录每次写入 _QUOTE_SERVED 的 (线程名, 来源)
        real_served = d._QUOTE_SERVED

        class _ServedLog(dict):
            def __setitem__(self, k, v):
                super().__setitem__(k, v)
                served_log.append((_t.current_thread().name, v["from"]))

        logged = _ServedLog()
        real_acquire = d._inflight_acquire

        def acquire(key):
            evt, other = real_acquire(key)
            if evt is None:                 # 没抢到 = 正在等待别人
                b_waiting.set()
            return evt, other

        def run(name):
            results[name] = d.get_stock_quote("600519")

        with mock.patch.object(tp.requests, "get", side_effect=fake_get), \
             mock.patch.object(d, "_QUOTE_SERVED", logged), \
             mock.patch.object(d, "_inflight_acquire", side_effect=acquire):
            a = _t.Thread(target=run, args=("A",), name="A")
            a.start()
            self.assertTrue(entered.wait(5), "A 必须真的发起上游请求")

            b = _t.Thread(target=run, args=("B",), name="B")
            b.start()
            self.assertTrue(b_waiting.wait(5), "B 必须进入 single-flight 等待（不是自己去抓）")

            release.set()
            a.join(5)
            b.join(5)
            self.assertFalse(a.is_alive() or b.is_alive(), "两个线程都必须结束")

        self.assertEqual(self.calls, 1, "并发同一只证券只允许 1 次上游请求")
        self.assertIsNotNone(results["A"])
        self.assertIsNotNone(results["B"])
        self.assertEqual(results["A"], results["B"])

        by_thread = dict(served_log)
        self.assertEqual(by_thread.get("A"), "network", "A 是真正抓取的一方")
        self.assertEqual(by_thread.get("B"), "memory",
                         "B 等 A 写完后读内存 → memory，绝不是 file")
# ------------------ 文件缓存回退的来源标记（第四轮必改）
class TestFileFallbackSource(QuoteCacheCase):
    """file_fallback = "上游没成功，退回旧文件缓存"；file = "休市直接读盘"。"""

    def test_waiter_reads_file_cache_marked_fallback(self):
        """A 抓取失败 → B 等到的是旧文件缓存，必须记 file_fallback（不是 none/file）。"""
        import threading as _t

        self.seed_file_cache(price=1440.00)      # 预先有一份有效文件缓存
        entered, release, b_waiting = _t.Event(), _t.Event(), _t.Event()
        results, served_log = {}, []

        def fake_get(url, **kw):
            self.calls += 1
            entered.set()
            self.assertTrue(release.wait(5), "A 的上游请求必须被放行")
            raise real_requests.Timeout("upstream down")

        class _ServedLog(dict):
            def __setitem__(self, k, v):
                super().__setitem__(k, v)
                served_log.append((_t.current_thread().name, v["from"]))

        real_acquire = d._inflight_acquire

        def acquire(key):
            evt, other = real_acquire(key)
            if evt is None:
                b_waiting.set()
            return evt, other

        def run(name):
            results[name] = d.get_stock_quote("600519")

        with mock.patch.object(tp.requests, "get", side_effect=fake_get), \
             mock.patch.object(d, "_QUOTE_SERVED", _ServedLog()), \
             mock.patch.object(d, "_inflight_acquire", side_effect=acquire):
            a = _t.Thread(target=run, args=("A",), name="A")
            a.start()
            self.assertTrue(entered.wait(5))
            b = _t.Thread(target=run, args=("B",), name="B")
            b.start()
            self.assertTrue(b_waiting.wait(5), "B 必须进入 single-flight 等待")
            release.set()
            a.join(5)
            b.join(5)
            self.assertFalse(a.is_alive() or b.is_alive())

        self.assertEqual(self.calls, 1, "只允许 A 打一次上游")
        self.assertIsNotNone(results["B"], "B 应当拿到文件缓存里的有效行情")
        self.assertEqual(results["B"]["price"], 1440.00)
        by_thread = dict(served_log)
        self.assertEqual(by_thread.get("B"), "file_fallback",
                         "B 读的是旧文件缓存 → file_fallback，不能是 none")

        # 状态必须与实际可用性一致：有数据就 cached，但绝不是 live
        meta = d.quote_cache_meta("600519")
        self.assertEqual(meta["last_fetch_status"], "failed")
        self.assertIn(meta["status"], ("cached",))
        self.assertNotEqual(meta["status"], "live")

    def test_closed_market_stale_cache_refresh_failure_is_fallback(self):
        """休市 + 过期文件缓存 + 刷新失败：返回旧数据，但标 file_fallback 且保留原抓取时间。"""
        self.seed_file_cache(age_sec=30 * 86400, price=1400.00)
        with d._CACHE_LOCK:
            old_ts = d._load_cache()["quote_600519"]["fetched_at"]
        old_asof = d.datetime.fromtimestamp(old_ts).strftime("%Y-%m-%d %H:%M:%S")
        self.assertIsNotNone(old_asof)
        d._market_status = lambda: ("已收盘", False)   # 非交易时段

        with self.serve(exc=real_requests.Timeout("t")):
            q = d.get_stock_quote("600519")

        self.assertIsNotNone(q, "有旧缓存就必须返回旧数据，不能报无数据")
        self.assertEqual(q["price"], 1400.00)
        meta = d.quote_cache_meta("600519")
        self.assertEqual(meta["served_from"], "file_fallback")
        self.assertEqual(meta["last_fetch_status"], "failed")
        self.assertEqual(meta["status"], "cached")
        self.assertTrue(meta["is_stale"], "30 天前的快照必须标记为过期/降级")
        self.assertEqual(meta["asof"], old_asof, "fetched_at 必须是原始抓取时间，不得伪装成现在")
        self.assertNotEqual(meta["status"], "live")

    def test_closed_market_fresh_file_cache_stays_plain_file(self):
        """对照：休市且缓存不过期 → file（不是 fallback），本轮修复不能把两者混为一谈。"""
        self.seed_file_cache(age_sec=60, price=1453.00)
        d._market_status = lambda: ("已收盘", False)
        with self.serve():
            q = d.get_stock_quote("600519")
        self.assertEqual(q["price"], 1453.00)
        self.assertEqual(d.quote_cache_meta("600519")["served_from"], "file")
        self.assertEqual(self.calls, 0, "休市读盘不应打上游")


class TestApiContract(QuoteCacheCase):
    def test_exactly_eighteen_fields_no_metadata_leak(self):
        with self.serve():
            q = d.get_stock_quote("600519")
        self.assertEqual(sorted(q), sorted(TencentQuoteProvider.QUOTE_FIELDS))
        leaked = [k for k in q if k.startswith("_")]
        self.assertEqual(leaked, [], f"缓存元数据不得泄进 API 返回：{leaked}")

    def test_types_are_json_serializable_like_before(self):
        with self.serve():
            q = d.get_stock_quote("600519")
        json.dumps(q)                                  # 前端拿到的是同样的 JSON
        self.assertIsInstance(q["volume_lots"], int)
        self.assertIsInstance(q["price"], float)
        self.assertIsInstance(q["name"], str)


# ------------------------------------------- 截断报文：必须失败、不得缓存
class TestTruncatedSnapshot(QuoteCacheCase):
    """评审意见：MIN_PARTS(51) 与 parse_quote 实际用到的下标 57 不一致，
    51~57 字段的截断报文会被静默补 0 当成有效行情。这里把它钉死。"""

    def test_required_parts_constant_is_consistent(self):
        p = TencentQuoteProvider
        self.assertEqual(p.REQUIRED_PARTS, 58)
        self.assertEqual(p.REQUIRED_PARTS, max(i for _l, i in p.FIELD_INDEX) + 1)
        self.assertEqual(tp.MIN_PARTS, 51, "传输层旧门槛保持不动（既有测试锁着）")
        self.assertLess(tp.MIN_PARTS, p.REQUIRED_PARTS)

    def test_truncated_snapshots_raise_format_error(self):
        for n in (51, 52, 57):
            with self.subTest(fields=n):
                parts = snapshot(n=n).split("~")
                self.assertEqual(len(parts), n)
                with self.assertRaises(ProviderFormatError) as ctx:
                    TencentQuoteProvider().parse_quote(parts, "600519")
                self.assertIn("截断", str(ctx.exception))

    def test_fifty_eight_fields_boundary_is_still_rejected(self):
        """58 个片段仍不够：片段里最后一个"字段"其实是结尾 `";` 的一部分。

        也就是"58 个数据字段"需要 59 个片段才成立；58 片段 → 下标 57 不是数字，
        旧实现会把它补成 0 当有效成交额，现在必须拒绝。
        """
        parts = snapshot(n=58).split("~")
        self.assertEqual(len(parts), 58)
        with self.assertRaises(ProviderFormatError):
            TencentQuoteProvider().parse_quote(parts, "600519")
        with self.serve(snapshot(n=58)):
            self.assertIsNone(d.get_stock_quote("600519"))
        self.assertNotIn("quote_600519", d._IN_MEMORY_CACHE)
        self.assertNotIn("quote_600519", d._load_cache())

    def test_truncated_snapshot_never_becomes_quote_or_enters_cache(self):
        for n in (51, 52, 57):
            with self.subTest(fields=n):
                with self.serve(snapshot(n=n)):
                    q = d.get_stock_quote("600519")
                self.assertIsNone(q, f"{n} 字段的截断报文不得产出行情")
                self.assertNotIn("quote_600519", d._IN_MEMORY_CACHE)
                self.assertNotIn("quote_600519", d._load_cache())
                meta = d.quote_cache_meta("600519")
                self.assertEqual(meta["status"], "unavailable")
                self.assertEqual(meta["last_fetch_status"], "failed")

    def test_full_snapshot_at_required_boundary_is_accepted(self):
        """58 个**数据字段** + 1 个终止符 = 59 个 parts：能解析。
        （正好 58 个 parts 时，下标 57 是报文结尾的 `";`，会在数值校验处失败，
          见 test_truncated_snapshots_raise_format_error —— 那是"截断"，不是"缺值"。）"""
        text = snapshot(n=59)
        parts = TencentQuoteProvider().normalize(text, code="600519")[0]
        self.assertEqual(len(parts), 59)
        self.assertEqual(parts[57], "658000.00", "第 58 个数据字段（成交额）必须在位")
        with self.serve(text):
            q = d.get_stock_quote("600519")
        self.assertIsNotNone(q)
        self.assertEqual(len(q), 18)
        self.assertGreater(q["turnover"], 0)

    def test_old_implementation_would_have_accepted_truncated(self):
        """把差异写清楚：旧实现 57 字段时确实返回"有效"行情（turnover=0），
        新实现拒绝。这是本轮**有意收紧**，不是回归。"""
        parts = snapshot(n=57).split("~")
        old = old_parse(parts, "600519")
        self.assertIsNotNone(old, "旧实现在 57 字段时返回行情 → 截断混入的真实路径")
        self.assertEqual(old["turnover"], 0.0)
        self.assertGreater(old["price"], 0)
        with self.assertRaises(ProviderFormatError):
            TencentQuoteProvider().parse_quote(parts, "600519")


# ------------------------------------------------------- 成交额单位（亿元）
class TestTurnoverUnit(QuoteCacheCase):
    def test_fixture_turnover_is_yi_yuan_not_yuan(self):
        text = fixture_text()
        parts = TencentQuoteProvider().normalize(text, code="600519")[0]
        q = TencentQuoteProvider().parse_quote(parts, "600519")
        raw_wan = float(parts[57])                     # 上游单位：万元
        self.assertEqual(q["turnover"], round(raw_wan / 10000, 2))
        # 亿元 × 1e8 == 万元 × 1e4，两边都等于"元"；两位小数带来 ≤0.005 亿 = 5e5 元误差
        self.assertAlmostEqual(q["turnover"] * 1e8, raw_wan * 1e4, delta=1e6)
        self.assertAlmostEqual(q["turnover"], 33.16, places=2)

    def test_downstream_denominator_still_treats_it_as_yi_yuan(self):
        quote = {"code": "600519", "turnover": 33.16}   # 33.16 亿元
        amounts = {"main_force": 1e8, "super_large": 5e7, "large": 5e7,
                   "middle": 0, "small": 0}
        fake = SimpleNamespace(ok=lambda: True, data={"amounts_yuan": amounts})
        with mock.patch.object(d, "get_stock_quote", return_value=quote), \
             mock.patch.object(d._providers, "call", return_value=fake):
            out = d.get_stock_real_money_flow("600519")
        self.assertEqual(out["main_force"], 1.0)                       # 1e8 元 = 1 亿
        self.assertAlmostEqual(out["main_force_pct"], 3.02, places=2)  # 1/33.16 ≈ 3.02%

    def test_unit_comments_do_not_regress_to_yuan(self):
        import inspect
        src = inspect.getsource(TencentQuoteProvider)
        self.assertIn("parts[57] 是「万元」", src)
        self.assertIn("亿元", src)
        self.assertNotIn("对外统一成\"元\"", src)


# --------------------------------------------- 缓存状态转换（来源 ≠ 抓取结果）
class TestCacheStateTransitions(QuoteCacheCase):
    def test_first_fetch_is_network_then_memory_hit(self):
        with self.serve():
            d.get_stock_quote("600519")
            first = d.quote_cache_meta("600519")
            d.get_stock_quote("600519")
            second = d.quote_cache_meta("600519")
        self.assertEqual(self.calls, 1)
        self.assertEqual((first["served_from"], first["status"]), ("network", "live"))
        self.assertEqual((second["served_from"], second["status"]), ("memory", "cached"))
        self.assertEqual(second["last_fetch_status"], "ok",
                         "上次抓取成功 ≠ 这次是实时数据")

    def test_after_close_file_cache_is_cached_not_live(self):
        with self.serve():
            d.get_stock_quote("600519")
        d._IN_MEMORY_CACHE.clear()
        d._market_status = lambda: ("已收盘", False)
        with self.serve():
            q = d.get_stock_quote("600519")
        meta = d.quote_cache_meta("600519")
        self.assertEqual(self.calls, 1, "休市不得再打上游")
        self.assertIsNotNone(q)
        self.assertEqual(meta["served_from"], "file")
        self.assertEqual(meta["status"], "cached")
        self.assertEqual(meta["market_status"], "已收盘")

    def test_swr_returns_old_value_and_is_not_live(self):
        with self.serve():
            d.get_stock_quote("600519")
        d._IN_MEMORY_CACHE["quote_600519"]["time"] -= 31
        spawned = []
        with mock.patch.object(d, "_spawn_refresh",
                               side_effect=lambda k, fn, vf=None: spawned.append(k)):
            q = d.get_stock_quote("600519")
            meta = d.quote_cache_meta("600519")
        self.assertEqual(spawned, ["quote_600519"])
        self.assertEqual(q["price"], 1453.0, "SWR 先给旧值")
        self.assertEqual(meta["served_from"], "memory_stale")
        self.assertEqual(meta["status"], "cached", "旧值不得标成 live")
        self.assertEqual(meta["last_fetch_status"], "ok", "但上次抓取确实是成功的")

    def test_failed_fetch_without_cache_has_no_served_layer(self):
        with self.serve(exc=real_requests.Timeout("t")):
            self.assertIsNone(d.get_stock_quote("600519"))
        meta = d.quote_cache_meta("600519")
        self.assertEqual(meta["served_from"], "none")
        self.assertEqual(meta["last_fetch_status"], "failed")
        self.assertEqual(meta["status"], "unavailable")


# ----------------------------------------- 后台刷新：真的跑完，不靠 sleep
class TestBackgroundRefresh(QuoteCacheCase):
    def _expire(self, price=1453.00):
        with self.serve(snapshot(**{"3": f"{price:.2f}"})):
            d.get_stock_quote("600519")
        self.assertEqual(self.calls, 1)
        d._IN_MEMORY_CACHE["quote_600519"]["time"] -= 31
        return d._LAST_SUCCESS["quote_600519"]

    def _swr(self, text=None, exc=None):
        """触发一次 SWR，并 join 掉它派生的后台线程（确定性等待，不用 sleep）。"""
        threads = []
        real_thread = d.threading.Thread

        class _Recorder(real_thread):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                threads.append(self)

        with mock.patch.object(d.threading, "Thread", _Recorder), self.serve(text, exc):
            q = d.get_stock_quote("600519")
        for t in threads:
            t.join(5)
            self.assertFalse(t.is_alive(), "后台刷新 5 秒内必须结束")
        return q

    def test_refresh_success_updates_memory_file_and_fetched_at(self):
        old_ts = self._expire()
        q = self._swr(snapshot(**{"3": "1500.00"}))
        self.assertEqual(q["price"], 1453.0, "本次仍应返回旧值（SWR 语义）")
        self.assertEqual(self.calls, 2, "过期后必须且只能多打一次上游")
        mem = d._IN_MEMORY_CACHE["quote_600519"]
        self.assertEqual(mem["data"]["price"], 1500.0, "新数据必须写回内存")
        self.assertEqual(d._load_cache()["quote_600519"]["__data"]["price"], 1500.0,
                         "新数据必须写回文件缓存")
        self.assertGreater(d._LAST_SUCCESS["quote_600519"], old_ts, "fetched_at 必须更新")
        self.assertEqual(mem["time"], d._LAST_SUCCESS["quote_600519"])
        # 刷新完成后再取一次：命中内存，来源是 memory 而不是 network
        with self.serve():
            again = d.get_stock_quote("600519")
        self.assertEqual(again["price"], 1500.0)
        self.assertEqual(d.quote_cache_meta("600519")["served_from"], "memory")

    def test_refresh_failure_keeps_old_data_and_old_timestamp(self):
        old_ts = self._expire()
        q = self._swr(exc=real_requests.Timeout("t"))
        self.assertEqual(q["price"], 1453.0)
        self.assertEqual(self.calls, 2)
        self.assertEqual(d._IN_MEMORY_CACHE["quote_600519"]["data"]["price"], 1453.0,
                         "刷新失败不得清空/改写内存缓存")
        self.assertEqual(d._load_cache()["quote_600519"]["__data"]["price"], 1453.0)
        self.assertEqual(d._LAST_SUCCESS["quote_600519"], old_ts,
                         "刷新失败不得刷新 fetched_at")
        self.assertEqual(d.quote_cache_meta("600519")["last_fetch_status"], "failed")

    def test_background_success_does_not_turn_that_call_into_live(self):
        """后台刷新成功也不能把"这一次"返回的旧值标成实时。"""
        self._expire()
        self._swr(snapshot(**{"3": "1500.00"}))
        # 直接检查那次 SWR 调用记录的来源（_QUOTE_SERVED 在调用瞬间写入）
        self.assertEqual(d._QUOTE_SERVED["600519"]["from"], "memory_stale")


if __name__ == "__main__":
    unittest.main()
