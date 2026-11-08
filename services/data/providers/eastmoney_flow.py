# -*- coding: utf-8 -*-
"""东方财富资金流 Provider：三种模式，三套解析，互不套用。

| 模式 | 接口 | 报文 | 用途 |
|---|---|---|---|
| history | push2his .../fflow/daykline/get | `data.klines`：CSV 字符串，每行 6 段 | 个股近 N 日资金 |
| today | push2 .../fflow/kline/get | `data.klines`：CSV 字符串，每行 5 段（lmt 只认 1） | 个股当日快照 |
| market_snapshot | push2 .../ulist.np/get | `data.diff`：**JSON 对象数组**（f62/f66/f72/f78/f84 金额，f69/f75/f81/f87 占比） | 大盘多指数资金 |

为什么 market_snapshot 必须单独解析：`diff` 是"每个标的一条快照对象"，
不是逗号分隔的逐日序列。把它塞进 klines 解析器（split(",") 取下标）只会拿到
`{"f62": ...}` 这种字典的字符串键，而不会报错 —— 属于"看起来对、数全错"。

单位与字段口径（2026-11-08 真实报文核对，见 docs/V2.1_S2_EASTMONEY_FLOW_DESIGN.md §2）：
  - klines：日期, 主力, 小单, 中单, 大单[, 超大单]，单位**元**；
    列顺序是接口内部的固定顺序，**不跟随 fields2 请求顺序**（换序请求已证）。
  - ulist：f62 主力 / f66 超大单 / f72 大单 / f78 中单 / f84 小单（元）；
    f69/f75/f81/f87 是占比(%)，**不得当金额**。
  - asof 只来自数据源：klines 取行首日期；ulist 没有任何时间戳 → asof 恒为空串，
    由组装层用腾讯行情的时间补齐，绝不用 now() 顶替。
"""

import json

import requests

from ..base import Provider
from ..exceptions import (
    ProviderEmpty, ProviderFormatError, ProviderNetworkError, ProviderTimeout,
)
from ..normalizers import secid, yuan_to_yi

DAY_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
KLINE_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}

MODES = ("history", "today", "market_snapshot")


class EastMoneyFlowProvider(Provider):
    name = "eastmoney_flow"
    kind = "flow"
    timeout = 8.0

    # ------------------------------------------------------------ 请求
    def request(self, mode="history", code=None, days=20, indices=None, timeout=None):
        timeout = timeout or self.timeout
        if mode == "history":
            params = {
                "lmt": "0", "klt": "101", "secid": secid(code, "stock"),
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56",
            }
            text = self._http(DAY_URL, params, max(timeout, 10.0))
        elif mode == "today":
            params = {
                "secid": secid(code, "stock"),
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55",
                "lmt": "1", "klt": "101",
            }
            text = self._http(KLINE_URL, params, timeout)
        elif mode == "market_snapshot":
            rows = indices or []
            if not rows:
                raise ProviderFormatError(self.name, "market_snapshot 需要至少一个指数", url=ULIST_URL)
            params = {
                "fltt": "2",
                "fields": "f62,f66,f69,f72,f75,f78,f81,f84,f87",
                "secids": ",".join(secid(r["code"], "index") for r in rows),
            }
            text = self._http(ULIST_URL, params, timeout)
        else:
            raise ProviderFormatError(self.name, f"未知模式：{mode!r}（支持 {', '.join(MODES)}）")
        return text

    def _http(self, url, params, timeout):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            resp.encoding = "utf-8"
            text = resp.text
        except requests.Timeout as e:
            raise ProviderTimeout(self.name, f"请求超时（{timeout}s）", url=url, cause=e)
        except requests.RequestException as e:
            raise ProviderNetworkError(self.name, f"网络异常：{type(e).__name__}", url=url, cause=e)
        if not text:
            raise ProviderEmpty(self.name, "空响应体", url=url)
        return text

    # ------------------------------------------------------------ 解析
    def normalize(self, raw, mode="history", code=None, days=20, indices=None, **kw):
        if mode == "market_snapshot":
            return self._normalize_snapshot(raw, indices or [])
        if mode == "today":
            return self._normalize_today(raw)
        return self._normalize_history(raw, days)

    # history：逐日 CSV，元 → 亿元，最近 days 行
    def _normalize_history(self, raw, days):
        try:
            js = json.loads(raw)
        except (TypeError, ValueError) as e:
            raise ProviderFormatError(self.name, "JSON 解析失败", url=DAY_URL, cause=e)
        klines = ((js.get("data") or {}).get("klines")) or []
        if not klines:
            raise ProviderEmpty(self.name, "klines 为空", url=DAY_URL)

        rows = [str(k).split(",") for k in klines]
        rows = rows[-days:] if days and days > 0 else rows

        out = {"dates": [],
               "flows": {"super_large": [], "large": [], "middle": [], "small": []}}
        for row in rows:
            if len(row) < 5:
                continue                      # 短行整行跳过：宁可少一天，也不补 0
            try:
                small = yuan_to_yi(row[2])
                middle = yuan_to_yi(row[3])
                large = yuan_to_yi(row[4])
                main = yuan_to_yi(row[1])
                super_large = (yuan_to_yi(row[5]) if len(row) > 5
                               else round(main - large, 2))   # 老接口只到 f55 时反推
            except (TypeError, ValueError):
                continue                      # 坏数值整行跳过（旧实现会用 0 顶上）
            day = row[0]
            out["dates"].append(day[5:] if len(day) >= 10 else day)
            out["flows"]["small"].append(small)
            out["flows"]["middle"].append(middle)
            out["flows"]["large"].append(large)
            out["flows"]["super_large"].append(super_large)

        if not out["dates"]:
            raise ProviderEmpty(self.name, "没有可解析的逐日资金行", url=DAY_URL)
        out["unit"] = "亿元"
        out["is_mock"] = False
        return out, (rows[-1][0] if rows and rows[-1] and len(rows[-1][0]) >= 10 else "")

    # today：当日快照，保留"元"，占比由调用方用成交额算
    def _normalize_today(self, raw):
        try:
            js = json.loads(raw)
        except (TypeError, ValueError) as e:
            raise ProviderFormatError(self.name, "JSON 解析失败", url=KLINE_URL, cause=e)
        klines = ((js.get("data") or {}).get("klines")) or []
        if not klines:
            raise ProviderEmpty(self.name, "klines 为空", url=KLINE_URL)
        parts = str(klines[0]).split(",")
        if len(parts) < 5:
            raise ProviderFormatError(self.name, f"当日资金字段不足（{len(parts)} 段，至少 5 段）",
                                      url=KLINE_URL)
        try:
            main = float(parts[1])
            small = float(parts[2])
            middle = float(parts[3])
            large = float(parts[4])
        except (TypeError, ValueError) as e:
            raise ProviderFormatError(self.name, "当日资金金额不是数字", url=KLINE_URL, cause=e)
        return {
            "date": parts[0],
            "unit": "元",
            "amounts_yuan": {
                "main_force": main,
                "small": small,
                "middle": middle,
                "large": large,
                "super_large": main - large,      # 当日接口没有 f56，用主力 − 大单反推
            },
        }, parts[0]

    # market_snapshot：diff 是快照对象数组；金额取 f62/f66/f72/f78/f84
    def _normalize_snapshot(self, raw, indices):
        try:
            js = json.loads(raw)
        except (TypeError, ValueError) as e:
            raise ProviderFormatError(self.name, "JSON 解析失败", url=ULIST_URL, cause=e)
        diff = ((js.get("data") or {}).get("diff")) or []
        if not diff:
            raise ProviderEmpty(self.name, "diff 为空（没有任何指数快照）", url=ULIST_URL)

        out_rows = []
        total = {"main_force": 0.0, "super_large": 0.0, "large_order": 0.0,
                 "middle_order": 0.0, "small_order": 0.0}
        partial = False

        for i, item in enumerate(diff):
            label = (indices[i].get("name") or "") if i < len(indices) else ""
            if item.get("f12"):
                label = item.get("f14") or label
            try:
                main = yuan_to_yi(item.get("f62"))
                super_large = yuan_to_yi(item.get("f66"))
                large = yuan_to_yi(item.get("f72"))
            except (TypeError, ValueError):
                # 任一指数缺主力/超大/大单 → 整份快照不可用（与迁移前"宁可不显示"一致），
                # 不剔除坏行后继续累加，否则页面的"全市资金"会少算一个指数却不提示。
                raise ProviderFormatError(
                    self.name, f"第 {i + 1} 个指数缺主力/超大单/大单金额（f62/f66/f72）",
                    url=ULIST_URL)
            row_partial = False
            try:
                middle = yuan_to_yi(item.get("f78"))
                small = yuan_to_yi(item.get("f84"))
            except (TypeError, ValueError):
                middle = small = None         # 中/小单缺失 = 未知，不是 0
                row_partial = True
                partial = True
            out_rows.append({
                "name": label, "main_force": main, "super_large": super_large,
                "large_order": large, "middle_order": middle, "small_order": small,
                "partial": row_partial,
            })
            total["main_force"] += main
            total["super_large"] += super_large
            total["large_order"] += large
            if middle is not None:
                total["middle_order"] += middle
            if small is not None:
                total["small_order"] += small

        if not out_rows:
            raise ProviderEmpty(self.name, "所有指数快照都缺必需金额字段", url=ULIST_URL)

        total = {k: round(v, 2) for k, v in total.items()}
        if partial:
            total["middle_order"] = None
            total["small_order"] = None
        return {
            "indices": out_rows,
            "total": total,
            "partial": partial,
            "unit": "亿元",
        }, ""                                # ulist 不返回任何时间戳
