# -*- coding: utf-8 -*-
"""东方财富 F10 主要财务指标 Provider（S3-4A）。

原来的 `data_service.get_latest_financials()` 自己拼 URL、自己判断市场后缀、
自己解析字段，并且用一句 `except Exception: return None` 把"没有财报"和"接口挂了"
混成同一个 None。这里把它搬成一个 Provider，职责只有三件：请求、解析、报告来源。

## 接口事实（S3-0 真实报文 tests/fixtures/s3_0/em_f10_600519.raw）

```
GET https://datacenter-web.eastmoney.com/api/data/v1/get
  reportName=RPT_F10_FINANCE_MAINFINADATA
  columns=ALL
  filter=(SECUCODE="600519.SH")
  pageNumber=1  pageSize=1  sortColumns=REPORT_DATE  sortTypes=-1
```

响应：`{version, result:{pages,count,data:[{…165 个字段…}]}, success, message, code}`。
600519 的样本：财报期 2026-09-30、披露日 2026-10-31、165 个字段里 56 个为 null，
同时存在 `EPSJBTZ=0`、`B_FREE_SHARE=0` 两个**真实零**。
所以解析绝不能写 `value or 0` —— 那会把 56 个"不知道"变成 0，把缺失伪装成事实。

## 对外十个字段（与迁移前的 dict 完全一致）

| 业务字段 | F10 原始字段 | 说明 |
|---|---|---|
| report_date | REPORT_DATE | 取前 10 位，必须是合法日期 |
| report_name | REPORT_DATE_NAME | 如 "2026三季报" |
| eps | EPSJB | 基本每股收益（元），原样 |
| bps | BPS | 每股净资产（元），原样 |
| revenue | TOTALOPERATEREVE | 营业总收入（元），不换算成亿元 |
| revenue_yoy | TOTALOPERATEREVETZ | 营收同比（%），原值 |
| net_profit | PARENTNETPROFIT | 归母净利润（元） |
| profit_yoy | PARENTNETPROFITTZ | 净利润同比（%） |
| roe | ROEJQ | 净资产收益率（%） |
| notice_date | NOTICE_DATE | 披露日期，取前 10 位；**可缺失**（→ None），非法仍失败 |

不 round、不换单位、不补零；null 保持 None，真实 0 保持 0。
NaN / Infinity / -Infinity / 溢出（"1e309"）不是财务数据，直接 failed，不做任何替换。

## 状态语义

- `success`：HTTP 200 → JSON → `result.data` 是**非空列表**，`data[0]` 是对象，
  且 `REPORT_DATE` / `NOTICE_DATE` 可以解析成日期。个别指标为 null 仍是 success。
- `empty`（ProviderEmpty）：`result.data` 为 `[]` 或 `null` —— 接口正常，但这只股票没财报。
  与 failed 严格区分：empty 不能当失败重试，失败也不能当"没财报"。
- `failed`（ProviderTimeout / ProviderNetworkError / ProviderFormatError）：
  HTTP 非 200、超时、连接失败、JSON 非法、`result` 缺失或非对象、
  `result.data` 不是列表、`data[0]` 不是对象、REPORT_DATE 缺失或不可解析、
  数值字段不可解析或非有限（NaN/Infinity）、NOTICE_DATE 非空但非法。
注：NOTICE_DATE 缺失（null / "" / "-"）**不算失败**，返回 None，十个字段结构不变；
  它不是 EPS/营收/利润的必要条件，旧实现也只是给空字符串。

错误信息带 `field=XXX`，能直接定位到哪一个原始字段（例如 `field=REPORT_DATE`）。

## 不做的事

- 不做缓存：`fin_{code}` / 6 小时 TTL 仍由 `data_service._cached_get` 负责。
- 不把缓存回退包装成 success：Provider 只报告"这次请求发生了什么"。
- 不改估值模型：`get_stock_valuation` / `estimate_intrinsic_value` 的公式一行没动。
"""

import datetime as _dt
import math

import requests

from ..base import Provider
from ..exceptions import (
    ProviderEmpty, ProviderFormatError, ProviderNetworkError, ProviderTimeout,
)
from ..normalizers import stock_secucode

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_F10_FINANCE_MAINFINADATA"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/",
}

PROVIDER_NAME = "eastmoney_f10"

# 业务字段 → (F10 原始字段, 类型)
# 顺序即对外 dict 的顺序，方便测试逐字段对照。
FIELD_MAP = (
    ("report_date", "REPORT_DATE", "date"),
    ("report_name", "REPORT_DATE_NAME", "text"),
    ("eps", "EPSJB", "num"),
    ("bps", "BPS", "num"),
    ("revenue", "TOTALOPERATEREVE", "num"),
    ("revenue_yoy", "TOTALOPERATEREVETZ", "num"),
    ("net_profit", "PARENTNETPROFIT", "num"),
    ("profit_yoy", "PARENTNETPROFITTZ", "num"),
    ("roe", "ROEJQ", "num"),
    ("notice_date", "NOTICE_DATE", "date_opt"),
)

DASHES = ("-", "--", "—")


def _date_field(row, field, required=True):
    """日期字段：合法日期取前 10 位（YYYY-MM-DD）。

    required=True（REPORT_DATE）：缺失/空/非法一律 ProviderFormatError —— 没有报告期就没有财报。
    required=False（NOTICE_DATE）：缺失（null / "" / "-" / "--"）返回 None，财报照常成功；
        但**非空且非法**仍然报错，不拿 REPORT_DATE 或今天顶替。
    """
    value = row.get(field)
    if value is None:
        if required:
            raise ProviderFormatError(PROVIDER_NAME, f"field={field} 缺失（null）")
        return None
    text = str(value).strip()
    if not text or text in DASHES:
        if required:
            raise ProviderFormatError(PROVIDER_NAME, f"field={field} 为空：{value!r}")
        return None
    day = text[:10]
    try:
        _dt.date.fromisoformat(day)
    except ValueError:
        raise ProviderFormatError(PROVIDER_NAME, f"field={field} 不是合法日期：{value!r}")
    return day


def _num_field(row, field):
    """数值字段：null → None；真实 0 → 0；不 round、不换单位。

    NaN / Infinity / -Infinity / 溢出（"1e309"）一律 ProviderFormatError：
    非有限值不是财务数据，一旦放行会变成「available=True、内在价值=inf、偏离度=nan」，
    既污染估值也会让严格 JSON 序列化直接报错。不改成 0，也不给默认值。
    """
    value = row.get(field)
    if value is None:
        return None
    if isinstance(value, bool):          # bool 是 int 的子类，别把 True 当 1
        raise ProviderFormatError(PROVIDER_NAME, f"field={field} 是布尔值：{value!r}")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ProviderFormatError(PROVIDER_NAME, f"field={field} 不是有限数值：{value!r}")
        return value                     # 原样返回，保持上游精度
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or text in DASHES or text.lower() in ("null", "none"):
            return None
        try:
            num = int(text) if text.lstrip("+-").isdigit() else float(text)
        except ValueError:
            raise ProviderFormatError(PROVIDER_NAME, f"field={field} 不可解析：{value!r}")
        if isinstance(num, float) and not math.isfinite(num):
            raise ProviderFormatError(
                PROVIDER_NAME, f"field={field} 不是有限数值：{value!r}（解析为 {num!r}）")
        return num
    raise ProviderFormatError(PROVIDER_NAME, f"field={field} 类型不支持：{type(value).__name__}")


class EastMoneyF10Provider(Provider):
    """东方财富 F10：一只股票的最新一期主要财务指标。"""

    name = PROVIDER_NAME
    kind = "financials"
    timeout = 8.0

    # ---- 请求 ----
    def build_filter(self, stock_code):
        """`(SECUCODE="600519.SH")` —— 市场后缀由 normalizers.stock_secucode() 决定。"""
        try:
            secucode = stock_secucode(stock_code)
        except ValueError as e:
            raise ProviderFormatError(PROVIDER_NAME, f"非法股票代码：{stock_code!r}（{e}）")
        return f'(SECUCODE="{secucode}")', secucode

    def build_params(self, stock_code):
        cond, _ = self.build_filter(stock_code)
        return {
            "reportName": REPORT_NAME,
            "columns": "ALL",
            "filter": cond,
            "pageNumber": "1", "pageSize": "1",
            "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        }

    def _get(self, params):
        """唯一的网络出口（测试替换这里，不发真实请求）。"""
        try:
            resp = requests.get(URL, params=params, headers=HEADERS, timeout=self.timeout)
        except requests.Timeout as e:
            raise ProviderTimeout(self.name, f"请求超时（{self.timeout}s）", url=URL, cause=e)
        except requests.RequestException as e:
            raise ProviderNetworkError(self.name, f"网络请求失败：{e}", url=URL, cause=e)
        if resp.status_code != 200:
            raise ProviderFormatError(self.name, f"HTTP {resp.status_code}", url=URL)
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderFormatError(self.name, "JSON 解析失败", url=URL, cause=e)

    def request(self, stock_code):
        return self._get(self.build_params(stock_code))

    # ---- 解析 ----
    def normalize(self, raw, stock_code=None):
        if not isinstance(raw, dict):
            raise ProviderFormatError(self.name, f"响应不是 JSON 对象：{type(raw).__name__}")
        if raw.get("success") is False:
            raise ProviderFormatError(
                self.name,
                f"上游返回失败：code={raw.get('code')!r} message={raw.get('message')!r}")
        result = raw.get("result")
        if result is None:
            raise ProviderFormatError(self.name, "result 缺失")
        if not isinstance(result, dict):
            raise ProviderFormatError(self.name, f"result 不是对象：{type(result).__name__}")

        rows = result.get("data")
        if rows is None:
            # 东财 pageSize/pageNumber 越界时会回 data:null；对 F10 就是"没有记录"
            raise ProviderEmpty(self.name, "result.data=null：上游未返回财报记录")
        if not isinstance(rows, list):
            raise ProviderFormatError(self.name, f"result.data 不是列表：{type(rows).__name__}")
        if not rows:
            raise ProviderEmpty(self.name, "result.data=[]：无财报记录")

        row = rows[0]
        if not isinstance(row, dict):
            raise ProviderFormatError(self.name, f"data[0] 不是对象：{type(row).__name__}")

        data = {}
        for name, field, kind in FIELD_MAP:
            if kind == "date":
                data[name] = _date_field(row, field, required=True)
            elif kind == "date_opt":
                data[name] = _date_field(row, field, required=False)
            elif kind == "text":
                value = row.get(field)
                data[name] = "" if value is None else str(value)
            else:
                data[name] = _num_field(row, field)

        secucode = row.get("SECUCODE") or (stock_code and stock_secucode(stock_code)) or ""
        return data, data["report_date"], {
            "secucode": secucode,
            "report_type": row.get("REPORT_TYPE"),
            "row_fields": len(row),
            "source_report": REPORT_NAME,
        }
