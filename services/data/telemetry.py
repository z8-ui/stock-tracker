# -*- coding: utf-8 -*-
"""Provider 层遥测（进程内，重启清零）。

为什么和 data_service 的 SOURCE_STATS 分开：
  - 那里统计的是「缓存键」（market_flow / stockflow_hist_600519），
    一次命中可能根本没发请求；
  - 这里统计的是「真实上游请求」（tencent_quote / eastmoney_flow），
    每次都是实打实的一次 HTTP。
两者混在一起就会出现"缓存命中冒充请求成功"和"一次请求记两笔"。
本轮不做持久化（规划里属于 D5 阶段）。

同时把失败原因分成 status（超时/网络/格式/空/未注册），
而不是原来一句 "返回空数据或未通过校验"。
"""

import threading
import time

_LOCK = threading.RLock()
_STATS = {}
# 最多保留多少条最近错误，避免进程长跑把内存吃光
_MAX_ERRORS = 5


def record(provider, status, ms, error=None):
    """记录一次 Provider 调用。绝不抛异常：统计失败不能影响主流程。"""
    try:
        name = str(provider or "unknown")
        now = time.time()
        with _LOCK:
            st = _STATS.setdefault(name, {
                "calls": 0, "ok": 0, "empty": 0, "failed": 0, "unavailable": 0,
                "ms_total": 0.0, "last_ok": None, "last_error": "", "last_error_at": None,
                "last_status": "", "errors": [],
            })
            st["calls"] += 1
            st["ms_total"] += float(ms or 0)
            st["last_status"] = status
            if status in ("success", "partial", "stale"):
                st["ok"] += 1
                st["last_ok"] = now
            elif status == "empty":
                st["empty"] += 1          # 源活着但没数据：不是"失败"，也不是成功
            elif status == "unavailable":
                st["unavailable"] += 1
            else:
                st["failed"] += 1
            if error:
                msg = "%s: %s" % (error.get("type", "Error"), error.get("message", ""))
                st["last_error"] = msg[:200]
                st["last_error_at"] = now
                st["errors"] = (st["errors"] + [{"at": now, "status": status,
                                                 "msg": msg[:200]}])[-_MAX_ERRORS:]
    except Exception:
        pass


def snapshot():
    """给数据健康面板：每个 Provider 的调用次数、成功率、空率、耗时、最近错误。"""
    out = {}
    with _LOCK:
        for name, st in _STATS.items():
            calls = st["calls"] or 1
            out[name] = {
                "calls": st["calls"],
                "ok": st["ok"],
                "empty": st["empty"],
                "failed": st["failed"],
                "unavailable": st["unavailable"],
                "success_rate": round(st["ok"] / calls, 3),
                "empty_rate": round(st["empty"] / calls, 3),
                "avg_ms": round(st["ms_total"] / calls, 1),
                "last_ok_at": _ts(st["last_ok"]),
                "last_status": st["last_status"],
                "last_error": st["last_error"],
                "last_error_at": _ts(st["last_error_at"]),
                "recent_errors": [{"at": _ts(e["at"]), "status": e["status"],
                                   "msg": e["msg"]} for e in st["errors"]],
            }
    return out


def reset():
    """测试用：清空统计。"""
    with _LOCK:
        _STATS.clear()


def _ts(v):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(v)))
    except Exception:
        return None
