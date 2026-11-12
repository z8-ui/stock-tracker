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


# ---------------------------------------------------------------- secid
# 东方财富 secid = "市场.代码"，市场 1=沪、0=深/北。
# 为什么不用 market_prefix() 决定指数：000001 既是上证指数又是平安银行，
# 靠默认参数猜会把两者混成一个 secid（设计见 docs/V2.1_S2_EASTMONEY_FLOW_DESIGN.md §4.5）。

INDEX_SECIDS = {
    "000001": "1.000001",   # 上证指数
    "000300": "1.000300",   # 沪深300
    "000905": "1.000905",   # 中证500
    "000016": "1.000016",   # 上证50
    "000688": "1.000688",   # 科创50
    "399001": "0.399001",   # 深证成指
    "399006": "0.399006",   # 创业板指
    "399005": "0.399005",   # 中小板指
    "899050": "0.899050",   # 北证50
}

_SH_STOCK_PREFIXES = ("600", "601", "603", "605", "688", "689", "900")
_SZ_STOCK_PREFIXES = ("000", "001", "002", "003", "300", "301", "200")
_BJ_STOCK_PREFIXES = ("43", "83", "87", "88", "92")


def stock_market(code):
    """A 股股票代码 → 交易所（"SH" / "SZ" / "BJ"）。唯一一处代码段判定。

    北交所（43/83/87/88/92）必须单独判出来：它和深市一样用 secid 市场位 0，
    只看 secid 前缀会把北交所误当成深交所（S3-4 TD-03）。

    非法输入抛 ValueError —— 不静默归入某个市场。
    """
    text = str(code or "").strip()
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"非法股票代码：{code!r}")
    # 北交所先判：92 段和沪市 9 开头不同，但顺序写死更安全
    if text.startswith(_BJ_STOCK_PREFIXES):
        return "BJ"
    if text.startswith(_SH_STOCK_PREFIXES):
        return "SH"
    if text.startswith(_SZ_STOCK_PREFIXES):
        return "SZ"
    raise ValueError(f"无法确定市场：{code!r}")


def stock_secucode(code):
    """A 股股票代码 → 东方财富 F10 的 SECUCODE（"600519.SH"）。

    与 `secid()` 共用同一套代码段定义（`stock_market()`），不复制 startswith 判断。
    注意：`secid()` 的北交所市场位是 0，而 F10 要的是 ".BJ" 后缀 ——
    同一个"市场"在两个接口里有两种拼法，不能互相顶替。
    """
    text = str(code or "").strip()
    return f"{text}.{stock_market(text)}"


def secid(code, kind="stock"):
    """A 股代码 → 东方财富 secid（"市场.代码"）。

    kind="stock"：按代码段判市场；kind="index"：只认 INDEX_SECIDS 显式表。
    无法确定时抛 ValueError —— 不静默回退到某个市场，否则会安静地拿到别的标的。
    """
    text = str(code or "").strip()
    if kind == "index":
        hit = INDEX_SECIDS.get(text)
        if not hit:
            raise ValueError(f"未知指数代码：{code!r}（请先登记到 INDEX_SECIDS）")
        return hit
    if kind != "stock":
        raise ValueError(f"未知 kind：{kind!r}（只支持 stock / index）")
    market = stock_market(text)          # 与 stock_secucode() 同一套判定
    return f"1.{text}" if market == "SH" else f"0.{text}"


def yuan_to_yi(value):
    """金额字段：元 → 亿元（两位小数）。缺失抛 ValueError，绝不默认 0。"""
    if value is None or value == "":
        raise ValueError("missing monetary field")
    return round(float(value) / 100_000_000, 2)
