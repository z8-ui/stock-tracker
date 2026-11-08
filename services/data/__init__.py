# -*- coding: utf-8 -*-
"""数据源 Provider 体系（V2.1 S1 起）。

对外只需要两件事：
    from services.data import registry
    result = registry.call("tencent_quote", "600519")   # ProviderResult
或者兼容老代码的调用方式：
    from services.data.providers.tencent import TencentQuoteProvider

约定见 base.py；异常分类见 exceptions.py；解析工具见 normalizers.py。
"""

from .base import Provider, ProviderResult, STATUSES, OK_STATUSES      # noqa: F401
from .exceptions import (                                              # noqa: F401
    ProviderError, ProviderTimeout, ProviderNetworkError,
    ProviderFormatError, ProviderEmpty, ProviderUnavailable,
)
from . import registry                                                 # noqa: F401

__all__ = [
    "Provider", "ProviderResult", "STATUSES", "OK_STATUSES", "registry",
    "ProviderError", "ProviderTimeout", "ProviderNetworkError",
    "ProviderFormatError", "ProviderEmpty", "ProviderUnavailable",
]
