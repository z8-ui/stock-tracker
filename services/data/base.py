# -*- coding: utf-8 -*-
"""Provider 规范：统一的返回结构 + 模板方法。

设计原则（刻意保持很薄）：
  1. Provider 只做三件事：发请求、解析成领域结构、报告来源与时间。
     缓存、降级、业务判断都留在上层（data_service / services/*），不塞进 Provider。
  2. `fetch()` 是唯一的对外方法，模板方法负责计时与异常转换 ——
     子类只需要实现 `request()` 和 `normalize()`，不会各自写一套 try/except。
  3. 返回结构是 dict 友好的（ProviderResult.as_dict()），方便直接进 API 或日志，
     但**不替换**现有 API 的返回体：页面字段兼容由 data_service 这一层保证。

状态取值（与 V2.1 规划一致）：
  success       拿到了可用数据
  empty         请求成功但没有数据
  partial       部分字段缺失，但主要数据可用（本阶段暂无使用方）
  stale         只能是过期缓存（由缓存层标注，Provider 自己不产生）
  failed        请求失败 / 格式错误 / 未预期异常
  unavailable   数据源未注册或主动关闭
"""

import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from .exceptions import ProviderError
from . import telemetry

STATUSES = ("success", "empty", "partial", "stale", "failed", "unavailable")
OK_STATUSES = ("success", "partial", "stale")


@dataclass
class ProviderResult:
    """一次数据源调用的完整结果。

    data        领域结构（行情就是 ~ 切分后的 parts，资金流就是 {dates, flows...}）
    source      数据源标识（tencent / eastmoney ...）
    status      见模块文档
    asof        数据自身的时间（源给的，ISO 字符串），源没给就是空串 —— 绝不拿"现在"顶替
    fetched_at  本地抓取时刻（epoch 秒）
    is_stale    是否为过期数据（缓存层填；Provider 恒为 False）
    error       失败时的结构化原因（见 ProviderError.to_error）
    elapsed_ms  本次调用耗时，数据健康面板直接用
    """

    data: object = None
    source: str = ""
    status: str = "success"
    asof: str = ""
    fetched_at: float = field(default_factory=time.time)
    is_stale: bool = False
    error: Optional[dict] = None
    elapsed_ms: float = 0.0
    attempts: list = field(default_factory=list)   # call_chain 用：每个源的尝试结果
    # ---- S3-3 追加（全部带默认值，旧 Provider 不填也兼容）----
    missing: list = field(default_factory=list)     # 缺失项，如 ["pages:3", "pb"]
    notes: list = field(default_factory=list)       # 解析过程中的事实记录，如 ["negative_pe:90"]
    extra: dict = field(default_factory=dict)       # 结构化附加信息（total/fetched/duplicates…）

    def ok(self):
        return self.status in OK_STATUSES and self.data is not None

    def dimensions(self):
        """把状态拆成三个互不替代的维度（S3-3 D5）。

        fetch_status  上游这次到底成没成：success / empty / failed / not_attempted
        completeness  数据完整度：complete / partial / unknown
        freshness     新鲜度：fresh / stale / unknown（Provider 恒为 fresh，缓存层改）
        三者不得合并成一个 status：`partial` 不是 `success`，`empty` 不是 `failed`。
        """
        if self.status == "success":
            fetch, comp = "success", "complete"
        elif self.status == "partial":
            fetch, comp = "success", "partial"
        elif self.status == "empty":
            fetch, comp = "empty", "unknown"
        elif self.status == "unavailable":
            fetch, comp = "not_attempted", "unknown"
        else:
            fetch, comp = "failed", "unknown"
        comp = self.extra.get("completeness") or comp
        if self.is_stale:
            fresh = "stale"
        elif self.status in ("failed", "unavailable"):
            fresh = "unknown"
        else:
            fresh = "fresh"
        return {"fetch_status": fetch, "completeness": comp, "freshness": fresh}

    def as_dict(self):
        d = asdict(self)
        d["ok"] = self.ok()
        return d


class Provider:
    """数据源基类：只定义契约，不定义业务。"""

    name = "base"
    kind = ""          # quote / flow / valuation / policy ...
    timeout = 8.0

    # ---- 子类实现 ----
    def request(self, *args, **kwargs):
        """发请求，返回原始响应（文本/dict）。失败请抛 services.data.exceptions 里的类型。"""
        raise NotImplementedError(f"{self.name} 未实现 request()")

    def normalize(self, raw, *args, **kwargs):
        """原始响应 → (领域数据, asof)。结构不对请抛 ProviderFormatError。"""
        raise NotImplementedError(f"{self.name} 未实现 normalize()")

    # ---- 对外唯一入口 ----
    def fetch(self, *args, **kwargs):
        """统一包装：计时 + 异常转状态，调用方永远拿到 ProviderResult 而不是异常。

        normalize() 允许返回 `(data, asof)` 或 `(data, asof, extra)`；
        extra 里的 missing_pages / completeness=partial 会让状态变成 partial ——
        「部分成功」不能被报成 success（S3-3 D2/D5）。
        """
        t0 = time.time()
        try:
            raw = self.request(*args, **kwargs)
            out = self.normalize(raw, *args, **kwargs)
            if isinstance(out, tuple) and len(out) == 3:
                data, asof, extra = out
            else:
                data, asof = out
                extra = {}
            extra = extra or {}
        except ProviderError as e:
            elapsed = (time.time() - t0) * 1000
            telemetry.record(self.name, e.status, elapsed, e.to_error())
            return ProviderResult(None, self.name, e.status, "", t0, False,
                                  e.to_error(), elapsed)
        except Exception as e:                     # 未预期异常也不能漏成 500
            err = ProviderError(self.name, f"未预期异常 {type(e).__name__}: {e}"[:160], cause=e)
            elapsed = (time.time() - t0) * 1000
            telemetry.record(self.name, "failed", elapsed, err.to_error())
            return ProviderResult(None, self.name, "failed", "", t0, False,
                                  err.to_error(), elapsed)
        status = self._status_for(data, extra)
        elapsed = (time.time() - t0) * 1000
        telemetry.record(self.name, status, elapsed, None)
        return ProviderResult(data, self.name, status, asof or "", t0, False, None,
                              elapsed,
                              missing=list(extra.get("missing")
                                           or extra.get("missing_pages") or []),
                              notes=list(extra.get("notes") or []), extra=extra)

    @staticmethod
    def _status_for(data, extra):
        """success / empty / partial 的判定顺序：先看有没有数据，再看完整性。"""
        if data is None or data == [] or data == {}:
            return "empty"
        if extra.get("missing_pages") or extra.get("missing") \
                or extra.get("completeness") == "partial":
            return "partial"
        return "success"

    def describe(self):
        """给 /api/data-health 之类的地方展示用。"""
        return {"name": self.name, "kind": self.kind, "timeout": self.timeout}
