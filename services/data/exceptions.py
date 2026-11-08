# -*- coding: utf-8 -*-
"""统一的数据源异常。

为什么单独定义：data_service 里原来一律 `except: return None`，
失败原因、数据源、是否超时全部丢失，页面只能显示"暂无数据"，
数据健康面板也分不清"源挂了"和"源返回空"。
这里把失败分成几类，Provider 层统一转换，调用方按类型决定降级策略。
"""


class ProviderError(Exception):
    """所有数据源错误的基类：带 provider / 原因 / 请求地址，可安全序列化进日志。"""

    status = "failed"

    def __init__(self, provider, message, *, url="", cause=None):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.url = url
        self.cause = cause

    def to_error(self):
        """给 ProviderResult.error 用的结构化描述（不含堆栈，避免把内部细节吐到页面）。"""
        return {
            "type": type(self).__name__,
            "provider": self.provider,
            "message": self.message,
            "url": self.url,
            "cause": f"{type(self.cause).__name__}: {self.cause}" if self.cause else "",
        }


class ProviderTimeout(ProviderError):
    """请求超时（可能是网络慢，也可能是数据源整体不可用）。"""


class ProviderNetworkError(ProviderError):
    """连接失败 / DNS / 连接被重置等，请求根本没拿到响应。"""


class ProviderFormatError(ProviderError):
    """拿到响应但结构不对（字段缺失、报文截断、JSON 解析失败）。"""


class ProviderEmpty(ProviderError):
    """请求成功但没有数据：空报文、零条记录。与"失败"区分，用于判断是否可用缓存。"""

    status = "empty"


class ProviderUnavailable(ProviderError):
    """数据源未注册 / 主动关闭（例如模拟兜底被 ALLOW_MOCK_FALLBACK 关掉）。"""

    status = "unavailable"
