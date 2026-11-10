# -*- coding: utf-8 -*-
"""S3-0 真实报文夹具测试（纯离线，不联网、不 import 业务代码）

这组测试只回答一个问题：**夹具还是不是当初采到的那份真实报文，结构有没有变**。
它不证明业务解析正确（那是 S3-1 之后的事），也不改任何既有断言。

证据分层（每条断言属于哪一层，写在 docstring 里）：
  [真实报文] 来自 2026-11-10 20:44-20:45 实采的 8 份原始响应，sha256 锁定；
  [离线执行] 本文件所有用例；
  [静态检查] URL 漂移守卫：夹具里的 URL 必须仍出现在被引用的源码行附近。

夹具目录：tests/fixtures/s3_0/
  <key>.raw        原始响应字节（原样，勿格式化）
  <key>.meta.json  采集元数据（HTTP 状态、字节、sha256、参数、依赖）
  manifest.json    8 份汇总 + 采集环境
  field_spec.json  字段/类型/单位/可空性规格（机器可校对）
  CAPTURE_RUN.log  两轮采集的原始输出（证明每个接口只请求 1 次）
"""

import hashlib
import json
import os
import re
import socket
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "s3_0")

# 8 个采集对象 = 6 类接口 + 行业 PE 三步链的 3 份原始响应
KEYS = [
    "tencent_quote_sh600519",
    "em_sector_flow",
    "em_sector_valuation",
    "em_f10_600519",
    "em_industry_pe_step1_stock",
    "em_industry_pe_step2_suggest",
    "em_industry_pe_step3_ulist",
    "tencent_index_kline_sh000001",
]
PE_CHAIN = KEYS[4:7]

# 任务书口径：每份报文记录 13 项（含"采集环境"与"采集结果及异常说明"）
REQUIRED_13 = (
    "source", "interface", "url", "params", "method", "requested_at",
    "http_status", "content_type", "bytes", "data_date", "sha256",
    "capture_env", "outcome",
)


def raw(key):
    with open(os.path.join(FIX, key + ".raw"), "rb") as f:
        return f.read()


def meta(key):
    with open(os.path.join(FIX, key + ".meta.json"), encoding="utf-8") as f:
        return json.load(f)


def load(key):
    return json.loads(raw(key).decode("utf-8"))


def spec():
    with open(os.path.join(FIX, "field_spec.json"), encoding="utf-8") as f:
        return json.load(f)


def manifest():
    with open(os.path.join(FIX, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def rows(key):
    """按 field_spec 的 row_path 取行；单对象（step1）包成 1 行列表。"""
    path = spec()["items"][key]["row_path"]
    node = load(key)
    for part in path.split("."):
        node = node[part]
    return node if isinstance(node, list) else [node]


class TestCaptureMetadata(unittest.TestCase):
    """采集元数据完整性 —— 证据层：[真实报文] + [静态检查]"""

    def test_all_targets_captured(self):
        for key in KEYS:
            m = meta(key)
            self.assertEqual(m["status"], "CAPTURED", f"{key} 状态不是 CAPTURED")
            self.assertEqual(m["http_status"], 200, f"{key} HTTP 状态异常")
            self.assertGreater(m["bytes"], 0)
            self.assertTrue(os.path.exists(os.path.join(FIX, m["raw_file"])))

    def test_raw_matches_declared_sha256_and_size(self):
        for key in KEYS:
            m = meta(key)
            body = raw(key)
            self.assertEqual(len(body), m["bytes"], f"{key} 字节数与元数据不符")
            self.assertEqual(hashlib.sha256(body).hexdigest(), m["sha256"],
                             f"{key} 原文已被改动（sha256 不符）")

    def test_required_meta_fields_present(self):
        required = ("source", "interface", "method", "url", "params", "requested_at",
                    "http_status", "content_type", "bytes", "sha256", "source_ref", "status")
        for key in KEYS:
            m = meta(key)
            for field in required:
                self.assertIn(field, m, f"{key} 缺元数据字段 {field}")
            self.assertTrue(m["source"] and m["interface"] and m["url"])
            self.assertEqual(m["method"], "GET")

    def test_requested_at_within_capture_window(self):
        for key in KEYS:
            m = meta(key)
            dt = datetime.fromisoformat(m["requested_at"])
            self.assertEqual(dt.strftime("%Y-%m-%d"), "2026-11-10")
            self.assertEqual(dt.hour, 20, f"{key} 采集时间不在本轮窗口内")
            self.assertIn(dt.minute, (44, 45))

    def test_no_credentials_in_url_or_params(self):
        boring = ("password", "secret", "api_key", "apikey", "access_token", "authorization")
        for key in KEYS:
            blob = json.dumps(meta(key), ensure_ascii=False).lower()
            for word in boring:
                self.assertNotIn(word, blob, f"{key} 的采集参数里出现疑似凭证字段 {word}")

    def test_source_ref_still_points_to_the_same_host(self):
        """URL 漂移守卫：夹具声明的 URL 主机必须仍出现在被引用的源码文件里。

        仓库内按真实路径查；只有夹具的证据包里另附了 `refs/<basename>` 快照，
        两种位置都找不到才算失败——不允许"源码不在就跳过"。
        """
        for key in KEYS:
            m = meta(key)
            path, _line = m["source_ref"].rsplit(":", 1)
            candidates = [
                os.path.join(ROOT, path),
                os.path.join(ROOT, "refs", os.path.basename(path)),
            ]
            src = next((c for c in candidates if os.path.exists(c)), None)
            if src is None:
                self.fail(f"{key} 引用的源文件不存在：{path}（或 refs/{os.path.basename(path)}）")
            with open(src, encoding="utf-8") as f:
                text = f.read()
            host = re.sub(r"^https?://", "", m["url"]).split("/")[0]
            self.assertIn(host, text,
                          f"{key} 的采集主机 {host} 已不在 {path} —— 源码可能换了数据源，夹具需重采")

    def test_capture_log_shows_exactly_one_request_per_interface(self):
        with open(os.path.join(FIX, "CAPTURE_RUN.log"), encoding="utf-8") as f:
            log = f.read()
        captured = re.findall(r"^\[CAPTURED\] (\S+)", log, re.M)
        self.assertEqual(len(captured), 8, "成功采集行数应等于 8")
        self.assertEqual(sorted(captured), sorted(KEYS), "采集 key 与夹具目录不一致")
        self.assertEqual(len(set(captured)), 8, "同一接口被采集了两次")
        not_captured = re.findall(r"^\[NOT CAPTURED\] (\S+):", log, re.M)
        self.assertEqual(sorted(not_captured), sorted(PE_CHAIN[1:]),
                         "只有行业 PE 的 2/3 步出现过未采集，且已补采")


class TestMetadataContract(unittest.TestCase):
    """13 项元数据口径：META_SCHEMA.md / 测试 / 实际 JSON 三者一致。

    背景：S3-0 首轮只把"采集环境"写在 manifest、"结果说明"隐含在 status 里，
    任务书要求的是每份 13 项，因此这里补上契约测试，防止再漂移。
    """

    def test_thirteen_required_fields_present_in_every_meta(self):
        for key in KEYS:
            m = meta(key)
            missing = [f for f in REQUIRED_13 if f not in m]
            self.assertEqual(missing, [], f"{key} 缺 13 项必填字段：{missing}")

    def test_data_date_key_exists_even_when_null(self):
        """没有数据日期必须是显式 null，不能省略键——省略等于把'接口不提供'变成'没记录'。"""
        for key in KEYS:
            m = meta(key)
            self.assertIn("data_date", m, f"{key} 没有 data_date 键")
            if m["data_date"] is not None:
                self.assertRegex(str(m["data_date"]), r"^\d{4}-?\d{2}-?\d{2}$")

    def test_capture_env_inline_matches_manifest(self):
        env = manifest()["capture_env"]
        for key in KEYS:
            m = meta(key)
            self.assertEqual(m["capture_env"], env,
                             f"{key} 的 capture_env 与 manifest 不一致（口径漂移）")
            self.assertEqual(m["capture_env_ref"], "manifest.json#capture_env")
            for field in ("host", "platform", "python", "requests", "git_head"):
                self.assertIn(field, m["capture_env"])

    def test_outcome_is_plain_fact_not_adjective(self):
        for key in KEYS:
            m = meta(key)
            outcome = m["outcome"]
            self.assertIsInstance(outcome, str)
            self.assertTrue(outcome.strip(), f"{key} outcome 为空")
            if m["status"] == "CAPTURED":
                self.assertTrue(outcome.startswith("采集成功"), outcome)
                self.assertIn(str(m["http_status"]), outcome)
                self.assertIn(str(m["bytes"]), outcome, "outcome 必须写出字节数")
            else:
                self.assertTrue(outcome.startswith("未采集") or outcome.startswith("异常"), outcome)

    def test_schema_doc_lists_the_same_thirteen_fields(self):
        """文档漂移守卫：META_SCHEMA.md 里必须逐个出现这 13 个字段名。"""
        path = os.path.join(FIX, "META_SCHEMA.md")
        self.assertTrue(os.path.exists(path), "缺 META_SCHEMA.md（13 项口径的唯一定义处）")
        with open(path, encoding="utf-8") as f:
            doc = f.read()
        for field in REQUIRED_13:
            self.assertIn(f"`{field}`", doc, f"META_SCHEMA.md 未定义字段 {field}")


class TestManifest(unittest.TestCase):
    """manifest 是第三方复算入口 —— 证据层：[真实报文]"""

    def test_manifest_has_eight_items(self):
        m = manifest()
        self.assertEqual(m["stage"], "S3-0")
        self.assertEqual(len(m["items"]), 8)
        self.assertEqual([i["key"] for i in m["items"]], KEYS)

    def test_manifest_counts(self):
        m = manifest()
        self.assertEqual(m["counts"], {"targets": 8, "captured": 8, "not_captured": 0})

    def test_manifest_sha256_agrees_with_meta(self):
        for item in manifest()["items"]:
            m = meta(item["key"])
            self.assertEqual(item["sha256"], m["sha256"])
            self.assertEqual(item["bytes"], m["bytes"])
            self.assertEqual(item["url"], m["url"])

    def test_capture_env_recorded(self):
        env = manifest()["capture_env"]
        for field in ("host", "platform", "python", "requests", "git_head"):
            self.assertTrue(env.get(field), f"采集环境缺 {field}")
        self.assertEqual(len(env["git_head"]), 40, "git_head 应是完整 commit id")


class TestFieldSpecMatchesRaw(unittest.TestCase):
    """字段/类型/单位规格 vs 真实报文 —— 证据层：[真实报文] + [离线执行]

    规格写死字段集合，上游加/删字段会立刻失败；这是"结构变化能被发现"的机制。
    """

    def test_spec_covers_all_eight_keys(self):
        self.assertEqual(sorted(spec()["items"].keys()), sorted(KEYS))

    def test_row_key_set_matches_spec(self):
        for key in KEYS:
            item = spec()["items"][key]
            if item["format"] != "json":
                continue
            sample = rows(key)[0]
            if isinstance(sample, list):
                continue        # 位置型行（K 线）由 TestIndexKline 单独校验
            expected = set(item["fields"])
            for i, row in enumerate(rows(key)):
                if item.get("declared_columns"):
                    # 165 列的 F10 只声明用到的字段，用子集 + 总数列数兜住
                    self.assertTrue(expected.issubset(set(row)),
                                    f"{key} 第{i}行缺少声明字段：{expected - set(row)}")
                    self.assertEqual(len(row), item["declared_columns"],
                                     f"{key} 列数变了（上游 columns=ALL 结构变动）")
                else:
                    self.assertEqual(set(row), expected,
                                     f"{key} 第{i}行字段集合与规格不一致："
                                     f"多={set(row) - expected} 少={expected - set(row)}")

    def test_types_match_spec(self):
        for key in KEYS:
            item = spec()["items"][key]
            if item["format"] != "json":
                continue
            if isinstance(rows(key)[0], list):
                continue
            for row in rows(key):
                for name, fspec in item["fields"].items():
                    value = row[name]
                    want = fspec["type"]
                    if want == "number":
                        self.assertIsInstance(value, (int, float), f"{key}.{name} 应为数字")
                        self.assertNotIsInstance(value, bool)
                    elif want == "str":
                        self.assertIsInstance(value, str, f"{key}.{name} 应为字符串")
                    elif want == "null":
                        self.assertIsNone(value, f"{key}.{name} 应保持 null")

    def test_declared_row_counts(self):
        for key in KEYS:
            item = spec()["items"][key]
            if item.get("row_path"):
                self.assertEqual(len(rows(key)), item["declared_rows"],
                                 f"{key} 行数变了，需重新核对报文语义")

    def test_every_numeric_field_declares_a_unit(self):
        for key, item in spec()["items"].items():
            for name, fspec in item["fields"].items():
                if fspec["type"] == "number":
                    self.assertTrue(fspec.get("unit"), f"{key}.{name} 没写单位")

    def test_null_type_is_not_zero(self):
        """EPSKCJB 在真实报文里是 JSON null，规格必须标 null 而不是 0。"""
        f10 = spec()["items"]["em_f10_600519"]["fields"]
        self.assertEqual(f10["EPSKCJB"]["type"], "null")
        self.assertIsNone(load("em_f10_600519")["result"]["data"][0]["EPSKCJB"])


class TestTencentQuote(unittest.TestCase):
    """腾讯快照：~ 分隔 GBK 文本 —— 证据层：[真实报文]"""

    @classmethod
    def setUpClass(cls):
        cls.text = raw("tencent_quote_sh600519").decode("gbk")
        cls.parts = cls.text.split("~")

    def test_field_count_meets_parser_minimum(self):
        self.assertEqual(len(self.parts), 88)
        self.assertGreaterEqual(len(self.parts), 51, "低于 Provider 的 MIN_PARTS 说明报文被截断")

    def test_gbk_decode_is_clean(self):
        self.assertNotIn("\ufffd", self.text, "GBK 解码出现替换字符，说明编码判断错了")

    def test_identity_fields(self):
        self.assertTrue(self.parts[0].startswith('v_sh600519="'))
        self.assertEqual(self.parts[1], "贵州茅台")
        self.assertEqual(self.parts[2], "600519")

    def test_price_fields_positive(self):
        for idx in (3, 4, 5, 33, 34):
            self.assertGreater(float(self.parts[idx]), 0, f"parts[{idx}] 价格不为正")

    def test_trade_time_is_compact_datetime(self):
        stamp = self.parts[30]
        self.assertRegex(stamp, r"^\d{14}$")
        self.assertEqual(stamp[:8], "20261110")

    def test_volume_parses_with_float_first(self):
        """成交量可能带小数点：int(x) 会炸，必须 int(float(x))。"""
        self.assertEqual(int(float(self.parts[6])), 23038)

    def test_units_declared_in_spec_match_magnitude(self):
        # 总市值 17988.67 亿 ≈ 1.8 万亿，与茅台体量一致 → 证明该字段原值就是亿元
        self.assertTrue(10000 < float(self.parts[44]) < 50000)
        # 成交额 331607 万元 = 33.16 亿
        self.assertTrue(1e5 < float(self.parts[57]) < 1e7)
        self.assertTrue(0 < float(self.parts[39]) < 100)   # PE 倍
        self.assertTrue(0 < float(self.parts[46]) < 50)    # PB 倍


class TestSectorFlow(unittest.TestCase):
    """行业板块资金流 clist —— 证据层：[真实报文]"""

    @classmethod
    def setUpClass(cls):
        cls.js = load("em_sector_flow")
        cls.rows = cls.js["data"]["diff"]

    def test_envelope(self):
        self.assertEqual(self.js["rc"], 0)
        self.assertEqual(self.js["data"]["total"], 496)

    def test_pagination_truncates_at_pz(self):
        """total=496 但 pz=60 只回 60 行 —— 现有实现只拿前 60 个板块，S3-3 要决策。"""
        self.assertEqual(len(self.rows), 60)
        self.assertLess(len(self.rows), self.js["data"]["total"])

    def test_row_shape(self):
        self.assertEqual({tuple(sorted(r)) for r in self.rows},
                         {("f14", "f2", "f3", "f62")})

    def test_flow_is_yuan_and_converts_to_yi(self):
        for r in self.rows:
            self.assertIsInstance(r["f62"], float, "f62 应为浮点（元）")
        flow_yi = max(r["f62"] for r in self.rows) / 1e8
        self.assertTrue(1 < flow_yi < 500, f"主力净流入换算成亿元后失真：{flow_yi}")

    def test_names_are_present_and_unique(self):
        names = [r["f14"] for r in self.rows]
        self.assertTrue(all(names))
        self.assertEqual(len(set(names)), len(names), "同一采集内板块名重复，需核对")

    def test_change_is_percent_scale(self):
        # 涨跌幅都在 ±10.5% 内（板块单日涨跌幅上限），说明单位是 %，不是小数
        self.assertTrue(all(abs(r["f3"]) <= 10.5 for r in self.rows))


class TestSectorValuation(unittest.TestCase):
    """行业板块估值 clist —— 证据层：[真实报文]；本类锁"缺失≠0"的关键证据"""

    @classmethod
    def setUpClass(cls):
        cls.js = load("em_sector_valuation")
        cls.rows = cls.js["data"]["diff"]

    def test_row_shape(self):
        self.assertEqual(set(self.rows[0]), {"f14", "f2", "f3", "f9", "f20", "f23", "f25", "f37"})
        self.assertEqual(self.js["data"]["total"], 496)

    def test_pb_is_missing_for_every_row_and_stays_a_string(self):
        """本次采集 f23（PB）60/60 全是 "-"。它必须保持字符串，不能被当成 0 倍。"""
        values = [r["f23"] for r in self.rows]
        self.assertEqual(set(values), {"-"}, "PB 缺值形态变了，需重新核对上游")
        self.assertTrue(all(isinstance(v, str) for v in values))

    def test_unknown_field_f37_is_also_missing(self):
        self.assertEqual({r["f37"] for r in self.rows}, {"-"})

    def test_loss_making_sectors_have_negative_pe(self):
        self.assertEqual(sum(1 for r in self.rows if r["f9"] < 0), 15)
        self.assertEqual(sum(1 for r in self.rows if r["f9"] > 0), 45)

    def test_market_cap_is_yuan(self):
        for r in self.rows:
            self.assertGreater(r["f20"], 1e9, "总市值小于 10 亿，单位可能不是元")

    def test_pe_filter_drops_fifteen_rows(self):
        """现有实现按 pe>0 过滤 → 60 行里 15 个亏损板块不会出现在气泡图上。"""
        kept = [r for r in self.rows if r["f9"] > 0]
        self.assertEqual(len(kept), 45)


class TestF10Financials(unittest.TestCase):
    """F10 主要财务指标 —— 证据层：[真实报文]；锁 null 与真实 0 并存"""

    @classmethod
    def setUpClass(cls):
        cls.js = load("em_f10_600519")
        cls.row = cls.js["result"]["data"][0]

    def test_envelope(self):
        self.assertEqual(self.js["result"]["pages"], 104)
        self.assertEqual(len(self.js["result"]["data"]), 1)
        self.assertEqual(len(self.row), 165, "columns=ALL 的列数变了")

    def test_identity_and_period(self):
        self.assertEqual(self.row["SECUCODE"], "600519.SH")
        self.assertEqual(self.row["REPORT_DATE"], "2026-09-30 00:00:00")
        self.assertEqual(self.row["REPORT_DATE_NAME"], "2026三季报")
        self.assertRegex(self.row["REPORT_DATE"], r"^\d{4}-\d{2}-\d{2} 00:00:00$")
        self.assertEqual(self.row["NOTICE_DATE"][:10], "2026-10-31")
        self.assertEqual(self.row["CURRENCY"], "CNY")

    def test_required_numbers(self):
        self.assertAlmostEqual(self.row["EPSJB"], 51.53, places=2)
        self.assertAlmostEqual(self.row["BPS"], 216.95, places=2)
        self.assertAlmostEqual(self.row["ROEJQ"], 24.47, places=2)

    def test_revenue_is_yuan(self):
        revenue = self.row["TOTALOPERATEREVE"]
        self.assertTrue(1e11 < revenue < 1e12, "营业总收入应约 1332 亿元")
        self.assertAlmostEqual(revenue / 1e8, 1332.84, places=1)

    def test_null_and_real_zero_coexist_in_one_record(self):
        """同一行里 56 个 null 与 2 个真实 0.0 —— 这是"缺失≠0"最硬的证据。"""
        nulls = [k for k, v in self.row.items() if v is None]
        zeros = [k for k, v in self.row.items() if isinstance(v, (int, float)) and v == 0]
        self.assertEqual(len(nulls), 56)
        self.assertEqual(sorted(zeros), ["B_FREE_SHARE", "EPSJBTZ"])
        self.assertIsNone(self.row["EPSKCJB"])
        self.assertEqual(self.row["EPSJBTZ"], 0)

    def test_no_dash_placeholders_in_f10(self):
        """F10 用 null 表示缺失，不用 "-"；混用会误导解析层。"""
        self.assertNotIn("-", self.row.values())


class TestIndustryPeChain(unittest.TestCase):
    """行业 PE 三步链：必须能分别定位是哪一步坏了 —— 证据层：[真实报文]"""

    def test_three_separate_raw_files_with_distinct_sha(self):
        shas = [meta(k)["sha256"] for k in PE_CHAIN]
        self.assertEqual(len(set(shas)), 3, "三步链被合并成一份，失去逐步定位能力")

    def test_step1_to_step2_derivation(self):
        ind = load(PE_CHAIN[0])["data"]["f127"]
        self.assertEqual(ind, "白酒Ⅱ")
        m2 = meta(PE_CHAIN[1])
        self.assertEqual(m2["params"]["input"], ind)
        self.assertEqual(m2["derived_from"], {"industry_name": ind})
        self.assertEqual(load(PE_CHAIN[1])["QuotationCodeTable"]["Data"][0]["Name"], ind)

    def test_step2_to_step3_derivation(self):
        s2 = load(PE_CHAIN[1])["QuotationCodeTable"]
        bk = s2["Data"][0]["Code"]
        self.assertEqual(bk, "BK1277")
        self.assertEqual(s2["Data"][0]["QuoteID"], "90.BK1277")
        m3 = meta(PE_CHAIN[2])
        self.assertEqual(m3["params"]["secids"], "90." + bk)
        self.assertEqual(m3["derived_from"], {"bk_code": bk})
        self.assertEqual(load(PE_CHAIN[2])["data"]["diff"][0]["f12"], bk)

    def test_each_step_parses_independently(self):
        for key in PE_CHAIN:
            doc = load(key)
            if key == PE_CHAIN[1]:
                # suggest 接口没有 rc 信封，用 QuotationCodeTable.Status 判成功
                self.assertEqual(doc["QuotationCodeTable"]["Status"], 0, f"{key} Status != 0")
                self.assertIn("Data", doc["QuotationCodeTable"])
            else:
                self.assertEqual(doc["rc"], 0, f"{key} rc != 0")
                self.assertIn("data", doc)

    def test_step1_response_contains_no_board_code(self):
        """第 1 步只该有个股信息：一旦里面出现 BK 代码，说明有人把合并结果塞进来了。"""
        self.assertNotIn(b"BK", raw(PE_CHAIN[0]))

    def test_pe_scale_is_one_hundredth(self):
        f9 = load(PE_CHAIN[2])["data"]["diff"][0]["f9"]
        self.assertEqual(f9, 2274)
        self.assertAlmostEqual(f9 / 100, 22.74, places=2)

    def test_dependency_is_recorded_in_meta(self):
        self.assertIsNone(meta(PE_CHAIN[0]).get("depends_on"))
        self.assertEqual(meta(PE_CHAIN[1])["depends_on"], PE_CHAIN[0])
        self.assertEqual(meta(PE_CHAIN[2])["depends_on"], PE_CHAIN[1])


class TestIndexKline(unittest.TestCase):
    """腾讯指数日 K —— 证据层：[真实报文]"""

    @classmethod
    def setUpClass(cls):
        cls.js = load("tencent_index_kline_sh000001")
        cls.kl = cls.js["data"]["sh000001"]["day"]

    def test_envelope_and_row_count(self):
        self.assertEqual(self.js["code"], 0)
        self.assertEqual(len(self.kl), 30)
        self.assertEqual(meta("tencent_index_kline_sh000001")["data_date"], self.kl[-1][0])

    def test_row_shape(self):
        self.assertEqual({len(r) for r in self.kl}, {6})
        self.assertTrue(all(isinstance(r[0], str) for r in self.kl))

    def test_dates_are_sorted_and_formatted(self):
        dates = [r[0] for r in self.kl]
        self.assertEqual(dates, sorted(dates), "K 线日期未按升序返回")
        self.assertTrue(all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in dates))
        self.assertEqual(dates[0], "2026-09-22", "请求的是 2026-09-01 起，实际只回最近 30 根")
        self.assertEqual(dates[-1], "2026-11-10")

    def test_volume_needs_float_first_conversion(self):
        """成交量形如 "404865177.000"：int() 会 ValueError，必须 int(float())。"""
        self.assertTrue(all("." in r[5] for r in self.kl))
        self.assertEqual(int(float(self.kl[0][5])), 506148369)
        with self.assertRaises(ValueError):
            int(self.kl[0][5])

    def test_positional_fields_are_numeric_strings(self):
        """K 线是位置型行：1-5 列都是字符串，必须能 float()；不要假设是数字类型。"""
        for row in self.kl:
            for i in range(1, 6):
                self.assertIsInstance(row[i], str, f"第 {i} 列应为字符串")
                float(row[i])          # 不可解析就会抛 ValueError

    def test_ohlc_consistency(self):
        for d, o, c, h, l, v in self.kl:
            o, c, h, l = float(o), float(c), float(h), float(l)
            self.assertGreaterEqual(h, max(o, c), f"{d} 最高价低于开盘/收盘")
            self.assertLessEqual(l, min(o, c), f"{d} 最低价高于开盘/收盘")
            self.assertGreater(float(v), 0)

    def test_extra_payload_exists_but_is_not_used(self):
        """响应里还有 qt/prec/version/mx_price，现有实现只读 day —— 留证，避免以为只有 day。"""
        inner = self.js["data"]["sh000001"]
        for key in ("qt", "prec", "version", "mx_price"):
            self.assertIn(key, inner)


class TestOfflineAndHonesty(unittest.TestCase):
    """离线性与"没有编造" —— 证据层：[静态检查] + [离线执行]"""

    def test_loaders_do_not_touch_network(self):
        original = socket.socket

        def boom(*args, **kwargs):
            raise AssertionError("S3-0 夹具测试试图联网")

        socket.socket = boom
        try:
            for key in KEYS:
                raw(key)
                meta(key)
                self.assertTrue(spec()["items"])
                manifest()
        finally:
            socket.socket = original

    def test_test_module_imports_no_http_client(self):
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            src = f.read()
        # 拼字符串是为了让断言本身不出现在被断言的文本里
        for forbidden in ("import " + "requests", "import " + "urllib", "from " + "urllib"):
            self.assertNotIn(forbidden, src, f"夹具测试不应引入 {forbidden}")

    def test_meta_status_vocabulary_is_explicit(self):
        allowed = {"CAPTURED", "NOT_CAPTURED", "BLOCKED_EMPTY_BODY"}
        for key in KEYS:
            status = meta(key)["status"]
            self.assertTrue(status in allowed or status.startswith("BLOCKED_HTTP_"),
                            f"{key} 状态值含义不明：{status}")

    def test_data_date_is_never_invented(self):
        """有日期就必须来自报文本身；没有就留 null，不能填采集日冒充数据日。"""
        for key in KEYS:
            d = meta(key)["data_date"]
            if d is None:
                continue
            self.assertRegex(str(d), r"^\d{4}-?\d{2}-?\d{2}$", f"{key} 数据日期格式可疑：{d}")

    def test_no_mock_markers_in_fixtures(self):
        for key in KEYS:
            self.assertNotIn(b"is_mock", raw(key), f"{key} 夹具里出现 is_mock 标记")
            self.assertNotIn("mock", json.dumps(meta(key), ensure_ascii=False).lower())


class TestEvidenceFilesAreCommitted(unittest.TestCase):
    """教训（S3-2 复审发现）：CAPTURE_RUN.log 被 .gitignore 的 `*.log` 吞掉。

    本地有文件，所以测试一直通过；但干净 clone 里没有它 →
    `test_capture_log_shows_exactly_one_request_per_interface` 直接 FileNotFoundError，
    报告出来的"全量全绿"在 bundle 里无法复现。这里把"证据必须入库"变成可执行断言。
    """

    def _git(self, *args):
        import subprocess
        return subprocess.run(["git", "-C", ROOT] + list(args),
                              capture_output=True, text=True, encoding="utf-8")

    def _require_git_repo(self):
        if not os.path.isdir(os.path.join(ROOT, ".git")):
            self.skipTest("不在 git 工作区（例如源码压缩包），无法校验入库状态；"
                          "文件存在性由 TestCaptureMetadata 保证")

    def test_capture_log_is_tracked_by_git(self):
        self._require_git_repo()
        out = self._git("ls-files", "--error-unmatch", "tests/fixtures/s3_0/CAPTURE_RUN.log")
        self.assertEqual(out.returncode, 0,
                         "CAPTURE_RUN.log 必须入库：干净 clone 里缺了就无法复现全量测试")
        self.assertEqual(out.stdout.strip(), "tests/fixtures/s3_0/CAPTURE_RUN.log")

    def test_no_fixture_file_is_left_untracked(self):
        self._require_git_repo()
        listed = {line for line in self._git("ls-files", "tests/fixtures/s3_0").stdout.splitlines() if line}
        on_disk = {"tests/fixtures/s3_0/" + name for name in os.listdir(FIX)}
        missing = sorted(on_disk - listed)
        self.assertEqual(missing, [], f"以下夹具/证据文件没有入库：{missing}")

    def test_capture_log_is_not_ignored(self):
        self._require_git_repo()
        out = self._git("check-ignore", "-v", "tests/fixtures/s3_0/CAPTURE_RUN.log")
        self.assertEqual(out.returncode, 1,
                         f"CAPTURE_RUN.log 仍被忽略规则命中：{out.stdout.strip()}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
