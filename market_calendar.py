# -*- coding: utf-8 -*-
"""交易日历（法定节假日 / 调休补班）。

为什么单独一个文件
------------------
`_market_status` 原来只判断「周末」，于是国庆、春节期间会被当成交易日：
接口返回上一交易日的旧数据，页面照样标"交易中"，这就是把旧数据当实时数据。
但节假日安排每年由国务院发布、还会有调休，硬编码一份假的日历比不写更糟。

所以这里只做「机制」，不做「事实」：
  - 有 market_calendar.json 就按它判断（holidays / extra_trading_days）
  - 没有就退回只看周末，并在状态里明确 `calendar_configured=False`
  - 文件格式错误同样退回周末 + 报 calendar_error，不静默用半份日历

文件格式（UTF-8 JSON）：
    {
      "year": 2026,
      "source": "国务院办公厅 2026 年节假日安排通知",
      "holidays": ["2026-01-01", "2026-01-02"],
      "extra_trading_days": ["2026-02-14"]
    }
"""

import json
import os
from datetime import date, datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CALENDAR = os.path.join(_ROOT, "market_calendar.json")


def _parse_days(items):
    out = set()
    for x in items or ():
        try:
            out.add(date.fromisoformat(str(x)[:10]))
        except (ValueError, TypeError):
            continue
    return out


def load_calendar(path=None):
    """返回 {"holidays": set[date], "extra": set[date], "configured": bool,
             "source": str, "error": str|None}"""
    path = path or os.environ.get("MARKET_CALENDAR_FILE") or DEFAULT_CALENDAR
    cal = {"holidays": set(), "extra": set(), "configured": False,
           "source": "", "error": None, "path": path}
    if not os.path.exists(path):
        cal["error"] = "未配置 market_calendar.json（只按周末判断，节假日会误判为交易日）"
        return cal
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        cal["holidays"] = _parse_days(raw.get("holidays"))
        cal["extra"] = _parse_days(raw.get("extra_trading_days"))
        cal["source"] = str(raw.get("source") or "")
        cal["configured"] = bool(cal["holidays"] or cal["extra"])
        if not cal["configured"]:
            cal["error"] = "日历文件存在但没有有效日期"
    except (OSError, ValueError) as e:
        cal["error"] = f"日历文件无法解析：{type(e).__name__}"
    return cal


def is_trading_day(day, cal=None):
    """返回 (是否交易日, 原因)。调休补班日即便是周末也算交易日。"""
    cal = cal if cal is not None else load_calendar()
    if isinstance(day, datetime):
        day = day.date()
    if day in cal["holidays"]:
        return False, "节假日休市"
    if day in cal["extra"]:
        return True, "调休补班"
    if day.weekday() >= 5:
        return False, "周末休市"
    return True, "工作日"
