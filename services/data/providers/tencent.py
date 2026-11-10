# -*- coding: utf-8 -*-
"""腾讯行情 Provider（试点迁移的第一个数据源）。

为什么先搬它：
  - 它是全系统最底层的行情入口：get_stock_quote / get_market_flow /
    get_stock_real_money_flow（借 get_stock_quote 拿成交额）都走它；
  - 逻辑极薄（一次 GET + ~ 切分），不含缓存、不含业务判断，
    搬出去能立刻看出 Provider 规范是否够用；
  - 行为必须 100% 不变：旧实现失败一律返回 None，这里保持
    "抛异常 → fetch() 转成 failed/empty → data_service._tencent_quote 返回 None"。

刻意留在上层的东西：
  - 价格有效性（price>0 视为不可用）仍由 get_stock_quote 判断 —— 那是业务语义，
    不是"这个源返回了什么"。Provider 只保证结构完整（字段数够）。
"""

import requests

from ..base import Provider
from ..exceptions import ProviderEmpty, ProviderNetworkError, ProviderFormatError, ProviderTimeout
from ..normalizers import split_tencent_snapshot, parse_trade_time, market_prefix

# 传输层门槛（历史值，保持不动）：少于 51 个片段就当报文明显被截断、连请求都不该算成功。
# 注意它**不是**解析门槛 —— 18 个业务字段真正要读到下标 57，见下方 REQUIRED_PARTS。
MIN_PARTS = 51

UA = {"User-Agent": "Mozilla/5.0"}


class TencentQuoteProvider(Provider):
    """腾讯 qt.gtimg.cn 实时快照（个股 / 指数通用）。"""

    name = "tencent_quote"
    kind = "quote"
    timeout = 5.0
    url_template = "http://qt.gtimg.cn/q={market}{code}"

    def build_url(self, code):
        return self.url_template.format(market=market_prefix(code), code=code)

    def request(self, code, **kwargs):
        code = str(code or "").strip()
        if not code:
            raise ProviderFormatError(self.name, "股票代码为空", url="")
        url = self.build_url(code)
        try:
            resp = requests.get(url, headers=UA, timeout=self.timeout)
        except requests.Timeout as e:
            raise ProviderTimeout(self.name, f"请求超时（>{self.timeout}s）", url=url, cause=e)
        except requests.RequestException as e:
            raise ProviderNetworkError(self.name, "网络请求失败", url=url, cause=e)
        resp.encoding = "gbk"          # 腾讯返回 GBK，解错就是乱码
        text = resp.text or ""
        if not text.strip():
            raise ProviderEmpty(self.name, "响应为空", url=url)
        return text

    def normalize(self, raw, code=None, **kwargs):
        parts = split_tencent_snapshot(raw)
        if len(parts) < MIN_PARTS:
            raise ProviderFormatError(
                self.name, f"字段数不足（{len(parts)} < {MIN_PARTS}），报文可能被截断",
                url=self.build_url(code or ""))
        asof = parse_trade_time(parts[30]) if len(parts) > 30 else ""
        return parts, asof

    # ---- 业务字段解析（S3-1：从 data_service.get_stock_quote 搬进来） ----
    #
    # 这里只负责"把 ~ 快照变成 18 个业务字段"，不做价格有效性判断 ——
    # price<=0（停牌/无行情）由 get_stock_quote 判为不可用，那是业务语义。
    #
    # 下标是腾讯协议里的位置，不是猜的：0 未知、1 名称、2 代码、3 现价、4 昨收、
    # 5 今开、6 成交量(手，可能是 "43845.000")、30 行情时间、31 涨跌、32 涨跌%、
    # 33 最高、34 最低、38 换手率、39 PE、43 振幅、44 总市值(亿)、46 PB、
    # 47 52周高、48 52周低、57 成交额(万元)。
    #
    # ⚠️ 单位：parts[57] 是「万元」。除以 10000 得到的是「亿元」，不是「元」
    #    （1 亿 = 10000 万）。下游 get_stock_real_money_flow 的占比分母
    #    `turnover_yi * 1e8` 也按亿元折算，两边口径一致。
    FIELD_INDEX = (
        ("price", 3), ("pre_close", 4), ("open", 5),
        ("high", 33), ("low", 34),
        ("change", 31), ("change_pct", 32),
        ("pe", 39), ("pb", 46), ("turnover_rate", 38),
        ("market_cap", 44), ("turnover", 57),
        ("amplitude", 43), ("high_52w", 47), ("low_52w", 48),
    )

    # 业务解析真正需要的最小字段数 = 最大下标 + 1 = 58。
    # 注意与 MIN_PARTS(51) 的区别：51 是"传输层没明显被截断"的旧门槛（保持不动），
    # 58 是"18 个字段全都能读到"的门槛；51~57 的报文在旧实现里会把成交额静默补 0，
    # 那种"看起来正常的行情"正是要拒绝的，所以 parse_quote 单独卡 58。
    REQUIRED_PARTS = max(idx for _label, idx in FIELD_INDEX) + 1

    # get_stock_quote 对外暴露的 18 个字段（API 契约，顺序即历史顺序）
    QUOTE_FIELDS = (
        "name", "code", "price", "pre_close", "open", "high", "low",
        "change", "change_pct", "volume_lots", "turnover", "turnover_rate",
        "amplitude", "pe", "pb", "market_cap", "high_52w", "low_52w",
    )

    # 结构性必需的业务字段：为空/缺失即"没有行情"，不是"值未知"
    # （其余数值字段为空 → None，交由上层与前端显示"--"）
    REQUIRED_NUMERIC = ("price",)

    def parse_quote(self, parts, code):
        """把 ~ 快照解析成 18 字段业务字典。

        空值语义（本轮修正）：**空字段 ≠ 真实零值**。
          - 字段存在但为空（`""`）→ `None`（该源没给这个值）
          - 字段是 `"0"` → `0.0`（真实零，例如亏损股 PE=0、停牌成交额=0）
          - 字段有值但不是数字 → `ProviderFormatError`
          - 报文不足 REQUIRED_PARTS / 结构性必需字段（price）为空 → `ProviderFormatError`

        与迁移前（`git show 0f7afe1:data_service.py`）的差别：旧实现把空字段补成 0，
        这里改成 None；其余取值表达式与守卫完全一致（见 scripts/verify_old_baseline.py）。
        """
        if len(parts) < self.REQUIRED_PARTS:
            raise ProviderFormatError(
                self.name,
                f"报文被截断：只有 {len(parts)} 个字段，行情解析需要 ≥{self.REQUIRED_PARTS}"
                f"（成交额在下标 {self.FIELD_INDEX[-1][1]}）。截断报文不得当成有效行情",
                url=self.build_url(code))
        if len(parts) < 2:
            raise ProviderEmpty(self.name, "快照字段不足以解析行情", url=self.build_url(code))

        def num(label, idx, required=False):
            if idx >= len(parts):
                # 理论上不可达（上面已卡 REQUIRED_PARTS），但绝不靠"补 0"蒙过去
                raise ProviderFormatError(
                    self.name, f"缺少必需字段 {label}(下标 {idx})", url=self.build_url(code))
            raw = parts[idx]
            if raw is None or raw == "":
                # 结构性必需（现价）为空 = 这份报文没有行情；可选字段为空 = 源没给值
                if required:
                    raise ProviderFormatError(
                        self.name, f"必需字段 {label}(下标 {idx}) 为空，无法判定行情",
                        url=self.build_url(code))
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                raise ProviderFormatError(
                    self.name, f"{label}(下标 {idx}) 不是数字：{raw!r}",
                    url=self.build_url(code))

        def lots(idx):
            # 腾讯成交量是带小数的字符串（"43845.000"），int() 直接炸 → 必须 float 再 int
            if idx >= len(parts):
                raise ProviderFormatError(
                    self.name, f"缺少必需字段 volume_lots(下标 {idx})", url=self.build_url(code))
            raw = parts[idx]
            if raw is None or raw == "":
                return None
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                raise ProviderFormatError(
                    self.name, f"volume_lots(下标 {idx}) 不是数字：{raw!r}",
                    url=self.build_url(code))

        quote = {"name": parts[1], "code": code}
        for label, idx in self.FIELD_INDEX:
            quote[label] = num(label, idx, required=(label in self.REQUIRED_NUMERIC))
        quote["volume_lots"] = lots(6)
        # 成交额：腾讯给"万元"，对外统一成"亿元"（1 亿 = 10000 万），保留两位；
        # 成交额为空时保持 None —— 不要拿 None/10000 去造一个 0 亿元的"有效"成交额
        if quote["turnover"] is not None:
            quote["turnover"] = round(quote["turnover"] / 10000, 2)
        return {f: quote[f] for f in self.QUOTE_FIELDS}
