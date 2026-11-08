# -*- coding: utf-8 -*-
"""原始响应 → 领域结构的纯函数。

只放"无状态、可单测、多方复用"的解析工具，不放业务判断。
"""

import datetime as _dt


def to_float(value, default=None):
    """安全转 float：'43845.000' / '' / None / '-' 都能吃，失败返回 default。

    腾讯的成交量字段就是带小数点的字符串（"43845.000"），
    以前直接 int() 会 ValueError 并被外层 except 静默吞掉。
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", "--", "null", "None"):
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def to_int(value, default=0):
    n = to_float(value)
    return default if n is None else int(n)


def split_tencent_snapshot(text):
    """腾讯快照报文（~ 分隔的一整行）→ 字段列表。空输入返回 []。"""
    if not text:
        return []
    return str(text).split("~")


def parse_trade_time(value):
    """源给的交易时间 → ISO 字符串；拿不到就是空串。

    支持 '20261106150000'（腾讯 8/14 位）与 '2026-11-06 15:00:00'。
    刻意不返回"现在"：源不给时间时上层宁可不显示，也不要编一个日期。
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = text.replace("/", "-")
    if len(text) >= 14 and text[:14].isdigit():
        try:
            return _dt.datetime.strptime(text[:14], "%Y%m%d%H%M%S").isoformat()
        except ValueError:
            return ""
    if len(text) == 8 and text.isdigit():
        try:
            return _dt.datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return ""


def market_prefix(code, sh_zero_index=True):
    """A 股代码 → 交易所前缀。

    sh_zero_index=True 时 000001 视为上证指数（与 data_service 既有行为一致），
    个股场景请传 False。
    """
    code = str(code or "").strip()
    if code.startswith(("6", "9")):
        return "sh"
    if sh_zero_index and code == "000001":
        return "sh"
    return "sz"
