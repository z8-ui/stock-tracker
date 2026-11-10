#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3-1 交付证据：迁移前后 18 个业务字段对照（离线，只用真实夹具）。

旧实现 = S3-1 之前 data_service.get_stock_quote() 里的解析段（原样复制，
用 `git show 0f7afe1:data_service.py` 可核对）；新实现 = TencentQuoteProvider.parse_quote()。
两者对同一份真实报文分别求值，逐字段打印，任何差异都会被标成 ✗ 并让脚本以非 0 退出。
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from services.data.providers.tencent import TencentQuoteProvider   # noqa: E402
from services.data.providers import tencent as tp                  # noqa: E402

FIX = os.path.join(REPO, "tests", "fixtures", "s3_0")
KEY = "tencent_quote_sh600519"


def old_parse(parts, stock_code):
    """S3-1 前的实现（逐行复制，含原来的 except 吞异常行为）。"""
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


FIELD_NOTE = {
    "name": "证券名称（parts[1]）",
    "code": "入参代码（不取响应里的 parts[2]）",
    "price": "现价 parts[3]",
    "pre_close": "昨收 parts[4]",
    "open": "今开 parts[5]",
    "high": "最高 parts[33]",
    "low": "最低 parts[34]",
    "change": "涨跌额 parts[31]",
    "change_pct": "涨跌幅% parts[32]",
    "volume_lots": "成交量（手）parts[6]，字符串转 int",
    "turnover": "成交额（**亿元**）= parts[57](万元)/10000，保留 2 位",
    "turnover_rate": "换手率% parts[38]",
    "amplitude": "振幅% parts[43]",
    "pe": "市盈率 parts[39]",
    "pb": "市净率 parts[46]",
    "market_cap": "总市值（亿）parts[44]",
    "high_52w": "52 周最高 parts[47]",
    "low_52w": "52 周最低 parts[48]",
}


def main():
    with open(os.path.join(FIX, KEY + ".raw"), "rb") as f:
        raw = f.read().decode("gbk")
    provider = TencentQuoteProvider()
    parts, asof = provider.normalize(raw, code="600519")
    old = old_parse(parts, "600519")
    new = provider.parse_quote(parts, "600519")

    print(f"夹具：tests/fixtures/s3_0/{KEY}.raw（{len(raw.encode('gbk'))} 字节，{len(parts)} 个 ~ 字段）")
    print(f"行情时间：{asof}；Provider.REQUIRED_PARTS={provider.REQUIRED_PARTS}，"
          f"normalize 门槛 MIN_PARTS={tp.MIN_PARTS}")
    print("单位约定：`turnover` 对外是**亿元**（parts[57] 为万元，除以 10000）")
    print()
    print("| # | 字段 | 含义/来源 | 迁移前（旧实现） | 迁移后（Provider） | 一致 |")
    print("|---:|---|---|---|---|---|")
    diff = 0
    for i, field in enumerate(provider.QUOTE_FIELDS, 1):
        a, b = old.get(field), new.get(field)
        same = a == b and type(a) is type(b)
        diff += 0 if same else 1
        print(f"| {i} | `{field}` | {FIELD_NOTE[field]} | {a!r} | {b!r} | {'✓' if same else '✗'} |")
    print()
    print(f"字段数：迁移前 {len(old)} / 迁移后 {len(new)}；不一致 {diff} 项")
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main())
