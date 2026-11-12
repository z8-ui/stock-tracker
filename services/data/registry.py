# -*- coding: utf-8 -*-
"""Provider 注册表：按名字取数据源、按链条降级。

为什么要有它：
  data_service 里现在到处是"直接 requests.get 某个 URL"，
  想知道"这个页面到底依赖哪些外部源"只能全文搜索。
  注册表把"有哪些源、各自负责什么"变成一处可列举的事实。

刻意不做的事：
  - 不做连接池、不做重试风暴：一次 fetch 最多走一遍链条，超时由 Provider 自己定；
  - 不做缓存：缓存是上层 _cached_get 的职责，混进来会让"过期数据"标记失真；
  - 不猜备用源：链条由调用方显式给出，没配就是没有。
"""

from .base import Provider
from .exceptions import ProviderUnavailable

_REGISTRY = {}


def register(provider, replace=False):
    """注册一个 Provider 实例。重复注册默认报错，避免悄悄覆盖别人的实现。"""
    if not isinstance(provider, Provider):
        raise TypeError(f"只能注册 Provider 实例，收到 {type(provider)!r}")
    if provider.name in _REGISTRY and not replace:
        raise ValueError(f"Provider 已注册：{provider.name}")
    _REGISTRY[provider.name] = provider
    return provider


def get(name):
    """按名字取 Provider；未注册抛 ProviderUnavailable（带可用名单，方便排查拼写）。"""
    provider = _REGISTRY.get(name)
    if provider is None:
        raise ProviderUnavailable(name, f"未注册的数据源，可用：{', '.join(sorted(_REGISTRY)) or '无'}")
    return provider


def names():
    return tuple(sorted(_REGISTRY))


def describe():
    return [p.describe() for p in _REGISTRY.values()]


def call(name, *args, **kwargs):
    """调用单个数据源，永远返回 ProviderResult（异常已在 Provider.fetch 里转成状态）。"""
    return get(name).fetch(*args, **kwargs)


def call_chain(provider_names, *args, **kwargs):
    """按顺序尝试多个数据源：第一个 ok 的就返回。

    全部失败时返回最后一次的结果，并在 error 里附上 attempts（每个源的状态），
    这样页面/日志能看出"主源超时、备用源空数据"这种组合，而不是一句"暂无数据"。
    """
    attempts = []
    last = None
    for name in provider_names:
        try:
            result = call(name, *args, **kwargs)
        except ProviderUnavailable as e:
            attempts.append({"source": name, "status": e.status, "error": e.to_error()})
            last = last or result_unavailable(e)
            continue
        attempts.append({
            "source": name, "status": result.status,
            "ok": result.ok(), "elapsed_ms": round(result.elapsed_ms, 1),
            "error": result.error,
        })
        if result.ok():
            result.attempts = attempts             # 命中的源 + 之前失败的源，都记下来
            return result
        last = result
    if last is None:
        last = result_unavailable(ProviderUnavailable(provider_names[0] if provider_names else "none",
                                                      "没有可用的数据源"))
    last.attempts = attempts
    return last


def result_unavailable(err):
    """把 ProviderUnavailable 转成统一结果（供 call_chain 在没有可用源时使用）。"""
    from .base import ProviderResult
    import time
    return ProviderResult(None, err.provider, err.status, "", time.time(), False, err.to_error())


# ---- 内置数据源注册 ----
# 生产环境目前只有腾讯行情一个 Provider；东财系列（资金流 / F10 / 行业 PE）
# 按 V2.1 规划在 S2-S3 逐个搬过来，搬一个注册一个，链条自然变长。
from .providers import tencent as _tencent      # noqa: E402
from .providers import eastmoney_flow as _emflow  # noqa: E402
from .providers import eastmoney_sector as _emsector  # noqa: E402
from .providers import eastmoney_f10 as _emf10      # noqa: E402

tencent_quote = register(_tencent.TencentQuoteProvider())
eastmoney_flow = register(_emflow.EastMoneyFlowProvider())
eastmoney_sector = register(_emsector.EastmoneySectorProvider())
eastmoney_f10 = register(_emf10.EastMoneyF10Provider())
