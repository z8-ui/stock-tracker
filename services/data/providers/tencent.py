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

# 腾讯快照至少这么多字段才够解析（调用方用到的最大下标是 48）
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
