"""
数据层 - 直接请求免费公开 API + 模拟兜底
数据来源：腾讯行情（稳定）+ 东方财富（辅助）+ 模拟数据（兜底）
"""

import requests
import hashlib
import json
import math
import random
import os
import time
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from policy_notes import get_policy_notes
from config import ALLOW_MOCK_FALLBACK, STALE_AFTER_SEC, VALUATION_SAFETY_MARGIN
from market_calendar import load_calendar, is_trading_day

# V2.1 S1：数据源访问开始走 Provider 体系（目前只搬了腾讯行情）
from services.data import registry as _providers

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/"
}

# ==============================================================
#  文件持久化缓存（重启不丢数据，休盘时段显示定格快照）
# ==============================================================

_CACHE_FILE = os.path.join(os.path.dirname(__file__), "_data_cache.json")

def _load_cache():
    """从文件加载缓存"""
    if not os.path.exists(_CACHE_FILE):
        return {}
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

# 文件缓存锁：RLock 是因为 _cache_update() 内部还会调用 _save_cache()，
# 同一线程要能重入；同时这把锁也保护 _IN_FLIGHT（见下面 _inflight_*）。
_CACHE_LOCK = threading.RLock()

def _save_cache(cache_dict):
    """保存整份缓存到文件（临时文件 + 原子替换）。

    注意：写单 key 请用 _cache_update()。整份覆盖只适合"我已经持有最新全量快照"的场景，
    否则并发下会把别人刚写进去的 key 覆盖掉。

    返回 True/False：写盘成功与否必须能被调用方识别（S3-3B r1）。
    失败时旧文件保持原样（tmp + atomic replace），不会破坏已有合法缓存。
    """
    try:
        with _CACHE_LOCK:
            tmp = _CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache_dict, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _CACHE_FILE)
        return True
    except Exception:
        return False


def _cache_update(cache_key, data, fetched_at, meta=None, drop_meta=False):
    """只更新一个 key：读-改-写全程持锁。

    修复的问题：原实现的 _cached_get 在请求开始时 _load_cache() 拿一份快照，
    请求结束再把整份快照写回。两个不同 cache_key 并发时，后写的会用旧快照
    把先写的那个 key 抹掉（lost update）——表现为"刚刷新的数据过一会儿又变回旧值"。

    S3-3B：可选 `meta`（旁路元数据）与数据在**同一次**读-改-写里落盘，
    避免"数据已更新、元数据还是旧的"被当成完整数据。业务数据本身的信封
    `{"__data": ..., "fetched_at": ...}` 一个字都不变。

    S3-3B r1：
      - `meta=None, drop_meta=True` → 同一次更新里删掉该 key 的旧元数据。
        元数据生成失败时必须走这里，否则"新数据 + 旧元数据"可能因内容相同而继续被信任。
      - 返回写盘结果（True/False）。失败时内存可能已更新、磁盘仍是旧数据，
        调用方必须据此把本次响应的完整性降级为 unknown。
    """
    with _CACHE_LOCK:
        cache = _load_cache()
        cache[cache_key] = {"__data": data, "fetched_at": fetched_at}
        store = cache.get(_META_NAMESPACE)
        if not isinstance(store, dict):
            store = {}
        if meta is not None:
            store[cache_key] = meta
        elif drop_meta:
            store.pop(cache_key, None)
        if store:
            cache[_META_NAMESPACE] = store
        elif _META_NAMESPACE in cache:
            cache.pop(_META_NAMESPACE, None)
        return _save_cache(cache)


# ==============================================================
#  旁路元数据（S3-3B）
#
#  业务数据保持裸结构（list 仍是 list，`{"__data": ..., "fetched_at": ...}`
#  信封一个字不改）；元数据放 `_data_cache.json` 的独立命名空间
#  `__data_meta__`，与业务 key 平级但永不混入数据本体。
#
#  为什么不是单独一个文件：数据与元数据必须能一次原子替换（否则崩溃后
#  会出现"新数据 + 旧元数据"），且现有测试用 patch `_load_cache/_save_cache`
#  隔离缓存 —— 同一份文件让隔离自动生效，不会把测试元数据写进真实目录。
#
#  绑定方式：元数据里存 `data_hash`（规范 JSON 的 sha256）。读取时重新算
#  当前缓存数据的 hash，不一致就整条作废 → 三个维度全部 unknown，
#  绝不让旧元数据冒充当前状态。
# ==============================================================

_META_NAMESPACE = "__data_meta__"
_META_SCHEMA = 1
LEGACY_MIGRATION_INTERVAL = 1800      # 旧缓存一次性安全刷新的冷却（秒）


def _fingerprint(data):
    """数据内容指纹：元数据靠它绑定"这份元数据描述的就是这份数据"。"""
    try:
        blob = json.dumps(data, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return ""
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _meta_record(cache_key):
    """读一条持久化元数据（不存在或形态不对都返回 None）。"""
    with _CACHE_LOCK:
        store = _load_cache().get(_META_NAMESPACE) or {}
        rec = store.get(cache_key) if isinstance(store, dict) else None
    return rec if isinstance(rec, dict) else None


def _cache_payload(cache_key):
    """读缓存条目 → (数据, 抓取时间)。旧格式（裸数据）没有抓取时间。"""
    with _CACHE_LOCK:
        raw = _load_cache().get(cache_key)
    if isinstance(raw, dict) and "__data" in raw:
        return raw.get("__data"), raw.get("fetched_at")
    return raw, None


def _freshness_of(fetched_at, served_from):
    """freshness 只看"这份数据有多旧"，和完整度是两件事。"""
    if served_from == "file_fallback":
        return "stale"
    if not fetched_at:
        return "unknown"
    status, trading = _market_status()
    limit = STALE_AFTER_SEC if trading else 3 * 86400
    try:
        age = time.time() - float(fetched_at)
    except (TypeError, ValueError):
        return "unknown"
    return "fresh" if age <= limit else "stale"


def _meta_trust(record, data, fetched_at):
    """元数据可信性判定 —— 唯一入口，data_meta 与 _needs_legacy_migration 共用。

    返回：
      ok          可信
      missing     没有元数据
      schema      版本不认识（不能拿当前规则解释旧/新格式）
      mismatch    data_hash 与当前数据不一致
      fetched_at  元数据不是这一轮抓取的（时间对不上 / 缺时间）

    r1 复审新增后两条：只比 hash 不够 —— 同样内容的两轮抓取 hash 相同，
    但完整性可能已经从 complete 变成 partial，不能继续复用旧结论。
    """
    if not isinstance(record, dict):
        return "missing"
    if record.get("schema") != _META_SCHEMA:
        return "schema"
    if not record.get("data_hash") or record.get("data_hash") != _fingerprint(data):
        return "mismatch"
    rec_ts = record.get("fetched_at")
    if rec_ts is None or fetched_at is None:
        return "fetched_at"
    try:
        if abs(float(rec_ts) - float(fetched_at)) > 1e-6:
            return "fetched_at"
    except (TypeError, ValueError):
        return "fetched_at"
    return "ok"


def data_meta(cache_key, served_from=None, trace=None, data=None):
    """S3-3B：把持久化旁路元数据 + 当前缓存数据合成对外三维度。

    返回字段（缺信息一律 unknown，绝不默认 complete）：
      meta_status    ok / missing / schema / mismatch / fetched_at / orphan / not_persisted
      fetch_status   success / failed / empty / not_attempted / unknown
      completeness   complete / partial / unknown
      freshness      fresh / stale / unknown
      rows/total     int 或 None（未知）
      missing        缺失项（分页号、duplicates:N、meta:* 等）
      fetched_at     epoch（本地抓取时刻，不是交易所时间）
      asof           上游数据时间；上游不提供时 "unknown"
      served_from    本次请求的数据来源（由 _cached_get 的 trace 传入）

    参数：
      data   本次**实际返回**的业务数据（r1）。写盘失败时磁盘还是旧数据，
             只有传进来才能保证 Headers 描述的是本次真正返回的这份数据。
      trace  _cached_get 的来源记录：fetch_failed / fetch_empty / persisted。

    `served_from == "file_fallback"` 表示本次抓上游失败、退回旧数据：
    freshness 必须是 stale，fetch_status 记本次失败（failed）。
    """
    persisted, persisted_at = _cache_payload(cache_key)
    record = _meta_record(cache_key)
    served = served_from or "none"
    trace = trace or {}
    served_data = data if data is not None else persisted
    # r2：优先用"本次实际返回的那份数据"的版本（trace["version"]），而不是磁盘条目的时间。
    # 两次读取之间别的请求可能已经更新了缓存：磁盘是 B，本次返回的是 A，
    # 拿 B 的元数据描述 A 就是跨请求错配（GPT 复现）。没有 version 的调用方才退回磁盘时间。
    if "version" in trace:
        version = trace.get("version")
    else:
        version = persisted_at
    out = {
        "cache_key": cache_key,
        "meta_status": "missing",
        "fetch_status": "unknown",
        "completeness": "unknown",
        "freshness": "unknown",
        "rows": (len(served_data) if isinstance(served_data, (list, dict))
                 else (1 if served_data is not None else None)),
        "total": None,
        "missing": [],
        "missing_pages": [],
        "notes": [],
        "duplicates": 0,
        "fetched_at": version,
        "fetched_at_iso": _fmt_ts(version) if version else None,
        "asof": "unknown",
        "source": "",
        "served_from": served,
        "is_mock": False,
        "data_hash": None,
    }

    if served_data is None:
        out["rows"] = None
        out["meta_status"] = "orphan" if record else "missing"
        if trace.get("fetch_failed"):
            out["fetch_status"] = "failed"      # 本次确实打了上游、并且失败了
        elif trace.get("fetch_empty"):
            out["fetch_status"] = "empty"       # 接口成功，但确实没有数据（≠ 接口挂了）
        return out

    out["data_hash"] = _fingerprint(served_data)

    # ---- r1：写盘失败 → 磁盘上没有能描述这份数据的记录，完整性/新鲜度一律 unknown ----
    if data is not None and trace.get("persisted") is False:
        out["meta_status"] = "not_persisted"
        out["missing"] = ["meta:not_persisted"]
        if trace.get("fetch_failed"):
            out["fetch_status"] = "failed"
        elif trace.get("fetch_empty"):
            out["fetch_status"] = "empty"
        elif served == "network":
            out["fetch_status"] = "success"     # 抓取本身成功，只是没能落盘
        return out

    status = _meta_trust(record, served_data, version)
    if status != "ok":
        out["meta_status"] = status
        out["missing"] = {
            "mismatch": ["meta:hash_mismatch"],
            "fetched_at": ["meta:fetched_at_mismatch"],
            "schema": ["meta:schema_unsupported"],
        }.get(status, [])
        # 本次抓取失败后退回旧数据：新鲜度与抓取结果必须如实反映降级
        if served == "file_fallback":
            out["freshness"] = "stale"
            out["fetch_status"] = "failed"
        return out

    record = record if isinstance(record, dict) else {}
    rows = len(served_data)
    completeness = str(record.get("completeness") or "unknown")
    total = record.get("total")
    if completeness == "complete" and (total is None or rows != total):
        # 防御：元数据自称完整，但行数对不上 total → 降级，不冒充完整
        completeness = "partial" if total is not None else "unknown"
    out.update({
        "meta_status": "ok",
        "fetch_status": record.get("fetch_status") or "unknown",
        "completeness": completeness,
        "rows": rows,
        "total": total,
        "missing": [str(m) for m in (record.get("missing") or [])],
        "missing_pages": list(record.get("missing_pages") or []),
        "notes": [str(n) for n in (record.get("notes") or [])],
        "duplicates": int(record.get("duplicates") or 0),
        "fetched_at": record.get("fetched_at") or persisted_at,
        "asof": (str(record.get("asof") or "").strip() or "unknown"),
        "source": record.get("source") or "",
        "is_mock": bool(record.get("is_mock")),
    })
    out["fetched_at_iso"] = _fmt_ts(out["fetched_at"]) if out["fetched_at"] else None
    out["freshness"] = _freshness_of(out["fetched_at"], served)
    if served == "file_fallback":
        out["fetch_status"] = "failed"      # 本次抓取失败后才退回的旧数据
    return out


# ==============================================================
#  Short-lived in-memory cache（交易时段 8 秒 TTL，减少重复外部调用）
#  2026-08 升级：SWR(过期先返回旧数据后台重建) + single-flight(并发只建一次)
# ==============================================================

_IN_MEMORY_CACHE = {}   # key -> {"data": ..., "time": fetched_at}
_IN_MEMORY_TTL = 8      # seconds

# 外部数据源调用统计（进程内，重启清零）——只给"数据健康"面板用，不参与业务判断
SOURCE_STATS = {}       # source -> {calls, ok, fail, ms_total, last_ok, last_error, last_error_at}
_SOURCE_LOCK = threading.Lock()


def record_source(source, ok, ms, error=""):
    """记录一次外部数据源调用结果（成功/失败、耗时、最近错误）。"""
    name = str(source or "unknown")
    now = time.time()
    try:
        with _SOURCE_LOCK:
            st = SOURCE_STATS.setdefault(name, {
                "calls": 0, "ok": 0, "fail": 0, "ms_total": 0.0,
                "last_ok": None, "last_error": "", "last_error_at": None})
            st["calls"] += 1
            st["ms_total"] += float(ms or 0)
            if ok:
                st["ok"] += 1
                st["last_ok"] = now
            else:
                st["fail"] += 1
                st["last_error"] = str(error or "")[:200]
                st["last_error_at"] = now
    except Exception:
        pass   # 统计失败绝不能影响主流程


def source_stats():
    """给 /api/data-health 的快照（成功率、平均耗时、最近错误）。"""
    out = {}
    with _SOURCE_LOCK:
        for name, st in SOURCE_STATS.items():
            calls = st["calls"] or 1
            out[name] = {
                "calls": st["calls"],
                "success_rate": round(st["ok"] / calls, 3),
                "avg_ms": round(st["ms_total"] / calls, 1),
                "last_ok_at": (_fmt_ts(st["last_ok"]) if st["last_ok"] else None),
                "last_error": st["last_error"],
                "last_error_at": (_fmt_ts(st["last_error_at"]) if st["last_error_at"] else None),
            }
    return out


# 缓存层统计（与 Provider 的上游统计分开：命中不等于发生了一次请求）
CACHE_STATS = {"hit": 0, "miss": 0, "stale_served": 0, "closed_served": 0, "refresh": 0}
_CACHE_STATS_LOCK = threading.Lock()


def _bump(key, n=1):
    try:
        with _CACHE_STATS_LOCK:
            CACHE_STATS[key] = CACHE_STATS.get(key, 0) + n
    except Exception:
        pass


def cache_stats():
    """缓存命中/未命中/降级/休市直出 —— 用来证明"缓存命中不算一次真实请求"。"""
    with _CACHE_STATS_LOCK:
        snap = dict(CACHE_STATS)
    total = snap["hit"] + snap["miss"] + snap["closed_served"] + snap["stale_served"]
    snap["total"] = total
    snap["hit_rate"] = round(snap["hit"] / total, 3) if total else 0.0
    return snap


def provider_stats():
    """Provider 层（真实上游请求）统计，见 services/data/telemetry.py。"""
    try:
        from services.data import telemetry
        return telemetry.snapshot()
    except Exception:
        return {}


def _fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return None
_IN_FLIGHT = {}         # key -> threading.Event，正在构建中的请求（single-flight）
_LAST_SUCCESS = {}      # key -> float，最后成功构建时间（/api/ready 硬过期判断用）


def _get_from_cache(key):
    """交易时段内存缓存：TTL 内返回已有结果，不调外部 API"""
    cached = _IN_MEMORY_CACHE.get(key)
    if cached and (time.time() - cached['time']) < _IN_MEMORY_TTL:
        return cached['data']
    return None


def _set_in_cache(key, data):
    _IN_MEMORY_CACHE[key] = {'data': data, 'time': time.time()}


def _inflight_acquire(cache_key):
    """原子地"检查 + 登记"。返回 (my_event, other_event)：

    - 抢到：返回 (Event, None)，调用方负责 release
    - 别人在跑：返回 (None, 别人的 Event)，调用方只能等
    """
    with _CACHE_LOCK:
        evt = _IN_FLIGHT.get(cache_key)
        if evt is not None:
            return None, evt
        evt = threading.Event()
        _IN_FLIGHT[cache_key] = evt
        return evt, None


def _inflight_release(cache_key, evt):
    with _CACHE_LOCK:
        _IN_FLIGHT.pop(cache_key, None)
    evt.set()


def _is_empty_result(value):
    """空结果判定（只看容器，不猜业务含义）。"""
    return value is None or (isinstance(value, (dict, list, tuple, str)) and len(value) == 0)


def _refresh_is_valid(fresh, validate_fn, existing):
    """后台刷新结果能不能覆盖现有缓存。

    2026-11 P1 修复：原来只写 ``validate_fn is None or validate_fn(fresh)``，
    validate_fn=None 时 ``None`` 也能过关 —— 一次失败的后台抓取会把内存缓存、
    文件缓存和 _LAST_SUCCESS 同时刷成 None，页面从"有数据"变成"暂无数据"。
    四条规则收在一处，和 _cached_get() 里的 _ok() 同口径：
      1. None 永远无效
      2. 有 validate_fn 以它为准；它自己抛异常也算无效
      3. 空结果不许覆盖非空的旧缓存（SWR 的意义是保住最后一次好数据）
      4. 其余放行
    """
    if fresh is None:
        return False
    if validate_fn is not None:
        try:
            if not validate_fn(fresh):
                return False
        except Exception:
            return False
    if _is_empty_result(fresh) and not _is_empty_result(existing):
        return False
    return True


def _spawn_refresh(cache_key, fetcher, validate_fn, meta_fn=None):
    """SWR 后台刷新：不阻塞当前请求，重建成功后更新内存 + 文件缓存。

    2026-11 修复三件事：
    1. 检查/登记/清理三者都走 _CACHE_LOCK（原来裸字典操作，两个请求可能同时刷新）
    2. 文件缓存写入与 _store() 同构：{"__data": ..., "fetched_at": ...}
       原来写成裸数据，重启后 _file_cache() 认不出来源时间 → 页面显示"抓取时间未知"
    3. 同时更新 _LAST_SUCCESS（/api/ready 的硬过期判断依赖它）

    S3-3B：`meta_fn` 与前台 _store() 用同一个函数生成旁路元数据 ——
    否则后台刷新会把 partial 数据写成"没有元数据"，重启后退化成 unknown。
    """
    evt, _ = _inflight_acquire(cache_key)
    if evt is None:
        return          # 已有刷新在跑，直接跳过

    def _refresh():
        try:
            fresh = fetcher()
            # 只有"确实拿到比旧缓存更好的数据"才允许写三处缓存
            existing = _IN_MEMORY_CACHE.get(cache_key, {}).get("data")
            if _refresh_is_valid(fresh, validate_fn, existing):
                ts = time.time()
                meta, meta_failed = None, False
                if meta_fn is not None:
                    try:
                        meta = meta_fn(fresh, ts)
                    except Exception:
                        meta = None
                    if not isinstance(meta, dict) or not meta:
                        meta, meta_failed = None, True
                if meta_fn is not None:
                    # r1b：先落盘、后发布。写盘失败时磁盘仍是旧数据 + 旧元数据，
                    # 此时若先把新数据写进 _IN_MEMORY_CACHE，响应会拿新数据配旧
                    # complete 元数据（GPT 复现：新 partial 被旧 complete 掩盖）。
                    if not _cache_update(cache_key, fresh, ts, meta=meta,
                                         drop_meta=meta_failed):
                        return
                else:
                    _cache_update(cache_key, fresh, ts)
                with _CACHE_LOCK:
                    _IN_MEMORY_CACHE[cache_key] = {"data": fresh, "time": ts}
                    _LAST_SUCCESS[cache_key] = ts
        except Exception:
            pass          # 后台刷新失败不影响前台：旧内存/文件缓存原样保留
        finally:
            _inflight_release(cache_key, evt)

    threading.Thread(target=_refresh, daemon=True).start()


def _get(url, params=None, timeout=8):
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        resp.encoding = "utf-8"
        return resp.text
    except:
        return None


# ==============================================================
#  市场交易时段检测 + 文件持久化缓存
# ==============================================================

def _market_status():
    """判断当前 A 股交易状态
    返回: ('交易中'|'午间休盘'|'已收盘'|'周末休市'|'节假日休市', is_trading)

    2026-11：接入 market_calendar（法定节假日/调休补班）。
    以前只判周末，国庆/春节期间会把上一交易日的快照当"交易中"返回。
    没有日历文件时退回只判周末，但 calendar_info() 会明确标记未配置。
    """
    now = datetime.now()
    cal = load_calendar()
    trading_day, day_reason = is_trading_day(now.date(), cal)
    if not trading_day:
        return (day_reason, False)
    h, m = now.hour, now.minute
    minutes = h * 60 + m
    if 9*60+15 <= minutes < 9*60+30:
        return ("集合竞价", True)
    if 9*60+30 <= minutes < 11*60+30:
        return ("交易中", True)
    if 11*60+30 <= minutes < 13*60:
        return ("午间休盘", False)
    if 13*60 <= minutes < 15*60:
        return ("交易中", True)
    return ("已收盘", False)


def calendar_info():
    """给 /api/health 和测试用：日历配置状态（configured=False 说明节假日判断不完整）。"""
    cal = load_calendar()
    return {
        "configured": cal["configured"],
        "source": cal["source"],
        "holidays": len(cal["holidays"]),
        "extra_trading_days": len(cal["extra"]),
        "error": cal["error"],
        "path": os.path.basename(cal["path"]),
    }


def yuan_to_yi(value):
    """金额字段：元 → 亿元，保留两位小数。

    只接受"源给的元"这一种口径，不再按数值大小猜单位 —— 旧实现
    `round(v/1e8,2) if abs(v) > 1e6 else round(v,2)` 会把百分比 -299 当成 -299 亿，
    也会把 100 万以下的真实净流入按"元"原样返回。
    缺失就抛异常，由调用方决定"这条记录不可用"，绝不默认填 0。
    """
    if value is None or value == "":
        raise ValueError("missing monetary field")
    return round(float(value) / 100_000_000, 2)


def _has_valid_data(data):
    """检查 dict 数据是否有真实值（非全零/全空）"""
    if not data:
        return False
    # 对于市场资金流：必须有真实的 fund flow 数据（主力资金非零或拆分项非零）
    if "main_force" in data:
        flow_fields = ["main_force", "super_large", "large_order", "middle_order", "small_order"]
        has_index = abs(data.get("index_price", 0)) > 0.01
        if not has_index:
            return False
        # 源明确返回过资金记录（diff 非空）→ 即使每一项都恰好是 0.00 亿，也是真实数据。
        # 只有"根本没拿到资金记录"时，才退回按数值非零判断（避免把空结果当行情）。
        if data.get("_flow_available") is True:
            return True
        vals = [data.get(f) for f in flow_fields if isinstance(data.get(f), (int, float))]
        return any(abs(v) > 0.01 for v in vals)
    return True


def _cached_get(cache_key, fetcher, validate_fn=None, source="", ttl=None, force=False,
                trace=None, meta_fn=None):
    """统一的缓存读写（SWR + single-flight + 数据标注注入）

    cache_key:    字符串标识
    fetcher:      无参函数，返回可 JSON 序列化的数据
    validate_fn:  可选，接收数据返回 bool，判断数据是否有效
    source:       数据源名称（如 "tencent"/"eastmoney"），注入返回数据
    ttl:          内存缓存秒数（默认 _IN_MEMORY_TTL）
    force:        为 True 时跳过全部缓存，强制同步拉取并写回缓存（手动更新按钮用）
    trace:        可选 dict；本函数在**实际决定返回数据的那条路径**里写入
                  {"served": "network|memory|stale|file|file_fallback|none"}，
                  调用方据此判断"这次是从哪拿到的"，不用靠调用前的缓存状态去猜。
    meta_fn:      可选 `fn(value, ts) -> dict`；写缓存时生成旁路元数据（S3-3B）。
                  只对需要三维度可验证的 key（板块）传，其它 key 不传即老行为。

    行为：
    - 交易时段：TTL 内命中内存直接返回；过期先返回旧数据并后台重建（SWR）；
      并发同 key 只允许一次真实构建（single-flight）；构建失败降级文件缓存
    - 非交易时段：优先文件缓存（定格快照），无缓存才抓一次
    - force=True：无视交易时段/缓存，直接抓取并写回（抓取失败仍降级文件缓存）
    - 返回的 dict 自动注入 _asof(数据获取时间)/_market_status/_source
    """
    status, is_trading = _market_status()
    ttl = ttl or _IN_MEMORY_TTL
    now = time.time()

    def _mark(where, version=None):
        """记录本次实际返回来源 + **该返回值所属的缓存版本**（r2）。

        version 是这条返回路径上那份数据的 fetched_at（内存/文件/网络各自的真实时间）。
        data_meta() 必须用它把"这份数据"和"描述这份数据的元数据"绑在一起；
        没有它就会出现"请求拿到旧 A、元数据却是新 B"的跨请求错配（GPT 复现）。
        """
        if trace is not None:
            trace["served"] = where
            trace["version"] = version
            trace["version_known"] = version is not None
        return where

    def _inject(value, asof):
        if isinstance(value, dict):
            # 浅拷贝：不把 _asof 写回缓存本体，否则同一份数据每读一次就把时间刷成"现在"
            value = dict(value)
            value["_asof"] = asof
            value["_asof_known"] = asof is not None
            age = (time.time() - asof) if asof else None
            value["_age_sec"] = round(age, 1) if age is not None else None
            # 陈旧判定：盘中 15 分钟；休盘时允许"最近一次收盘"的旧快照，
            # 但超过 3 天（例如缓存停在两个月前）必须标出来，不能当最新行情。
            if asof is None:
                stale, why = True, "缓存没有抓取时间（旧格式），无法确认新鲜度"
            else:
                limit = STALE_AFTER_SEC if is_trading else 3 * 86400
                stale = age > limit
                why = "" if not stale else f"缓存已 {age/60:.0f} 分钟未更新"
            value["_stale"] = stale
            value["_stale_reason"] = why
            value["_market_status"] = status
            if source:
                value["_source"] = source
        return value

    def _store(value, ts=None):
        ts = ts if ts is not None else now
        _IN_MEMORY_CACHE[cache_key] = {"data": value, "time": ts}
        _LAST_SUCCESS[cache_key] = ts
        # 旁路元数据与业务数据同一次落盘（S3-3B）；生成失败也不能影响主流程，
        # 但必须**同一次**把旧元数据删掉 —— 否则"新数据 + 旧元数据"可能因内容
        # 相同（hash 相同）继续被当成可信，把旧的 complete 复用到新一轮数据上（r1）。
        meta, meta_failed = None, False
        if meta_fn is not None:
            try:
                meta = meta_fn(value, ts)
            except Exception:
                meta = None
            if not isinstance(meta, dict) or not meta:
                meta, meta_failed = None, True
        # 落盘结构与 _spawn_refresh 完全一致：{"__data": ..., "fetched_at": ...}
        persisted = _cache_update(cache_key, value, ts, meta=meta,
                                  drop_meta=meta_failed)
        if trace is not None:
            trace["persisted"] = bool(persisted)
        return value

    def _file_cache():
        """返回 (注入标注后的值, 该缓存条目的 fetched_at)；无缓存返回 (None, None)。

        r2：把 fetched_at 一并交出去 —— 调用方要把它当作"这份数据的版本"记进 trace，
        供 data_meta() 校验元数据属于同一轮抓取。旧格式（裸 list）没有时间戳，版本为 None。
        """
        with _CACHE_LOCK:
            raw = _load_cache().get(cache_key)
        if raw is None:
            return None, None
        if isinstance(raw, dict) and "__data" in raw:
            val, fetched_at = raw["__data"], raw.get("fetched_at")
        else:
            # 旧格式（有数据、无时间戳）：不猜时间，asof=None 表示"抓取时间未知"
            val, fetched_at = raw, None
        if fetched_at:
            # 重启后把落盘的抓取时间补回内存，/api/ready 与前端时间戳才有依据
            _LAST_SUCCESS.setdefault(cache_key, fetched_at)
        return _inject(val, fetched_at), fetched_at

    def _ok(v):
        """None 永远不算有效数据：宁可显示"暂无数据"，也不能把 null 存进缓存。"""
        if v is None:
            return False
        return validate_fn is None or validate_fn(v)

    def _timed_fetch():
        """包一层计时：数据健康面板要"成功率 / 响应时间 / 最近错误"，只能在这里记。"""
        t0 = time.time()
        try:
            v = fetcher()
        except Exception as e:
            record_source(source or cache_key, False, (time.time() - t0) * 1000,
                          f"{type(e).__name__}: {str(e)[:120]}")
            raise
        good = _ok(v)
        record_source(source or cache_key, good, (time.time() - t0) * 1000,
                      "" if good else "返回空数据或未通过校验")
        return v

    # 强制刷新：跳过所有缓存层，同步拉取并写回
    if force:
        _bump("miss")
        fresh = _timed_fetch()
        if _ok(fresh):
            _mark("network", now)
            return _inject(_store(fresh), now)
        cached, cver = _file_cache()
        if cached is not None:
            _mark("file_fallback", cver)   # 强制刷新失败 → 返回旧缓存，同样是降级
            return cached
        _mark("none", None)
        return fresh

    if is_trading:
        mem = _IN_MEMORY_CACHE.get(cache_key)
        # 1) 新鲜内存 → 直接返回
        if mem and (now - mem["time"]) < ttl:
            _bump("hit")
            _mark("memory", mem["time"])
            return _inject(mem["data"], mem["time"])
        # 2) 过期内存 → SWR：先返回旧数据，后台异步重建
        if mem:
            _bump("stale_served")
            _bump("refresh")
            if meta_fn is None:
                _spawn_refresh(cache_key, _timed_fetch, validate_fn)
            else:
                _spawn_refresh(cache_key, _timed_fetch, validate_fn, meta_fn=meta_fn)
            _mark("stale", mem["time"])
            return _inject(mem["data"], mem["time"])
        # 3) 无内存 → single-flight 同步构建（并发只建一次，检查+注册原子化）
        evt, other = _inflight_acquire(cache_key)
        waiting = evt is None
        if waiting:
            evt = other
            evt.wait(5)  # 别人正在构建：最多等 5 秒
            mem = _IN_MEMORY_CACHE.get(cache_key)
            if mem:
                _bump("hit")
                _mark("memory", mem["time"])   # 等别人构建完再读内存 = 内存命中，不是读文件
                return _inject(mem["data"], mem["time"])
            cached, cver = _file_cache()
            if cached is not None:
                # 等到的是"别人没建成、只剩旧文件缓存"→ 这是降级，不是休市读盘
                _mark("file_fallback", cver)
                return cached
            _mark("none", None)
            return None
        try:
            _bump("miss")
            fresh = _timed_fetch()
            if _ok(fresh):
                _mark("network", now)
                return _inject(_store(fresh), now)
            # 构建失败（含网络异常）→ 文件缓存兜底
            cached, cver = _file_cache()
            if cached is not None:
                _mark("file_fallback", cver)
                return cached
            _mark("none", None)
            return fresh
        except Exception:
            # 抓取抛异常（超时/连接失败）不能把整个请求打成 500：
            # 有旧缓存就返回旧缓存（带 _stale），没有就返回 None 让路由给"暂无数据"
            cached, cver = _file_cache()
            if cached is not None:
                _mark("file_fallback", cver)
            else:
                _mark("none", None)
                if trace is not None:
                    trace["fetch_failed"] = True   # 让 X-Data-Fetch-Status 如实记 failed
            return cached if cached is not None else None
        finally:
            _inflight_release(cache_key, evt)

    # 非交易时段：优先文件缓存（定格快照）
    cached, cver = _file_cache()
    # 注意：缓存值可能是 list（板块资金流），list 没有 _stale 字段，不能直接 .get()
    if cached is not None and not (isinstance(cached, dict) and cached.get("_stale")):
        _bump("closed_served")
        _mark("file", cver)
        return cached
    # 无缓存，或缓存已经旧到不能代表"最近一次收盘"（例如停在两个月前）：
    # 先尝试抓一次；抓到就存，抓不到才退回旧缓存并保留 _stale 标记。
    try:
        fresh = fetcher()
    except Exception:
        fresh = None
    if _ok(fresh):
        _mark("network", now)
        return _inject(_store(fresh), now)
    if cached is not None:
        # 上游刷新没成功，退回的是旧快照：file_fallback（不是"休市直接读盘"）
        _mark("file_fallback", cver)
        return cached
    _mark("none", None)
    return fresh


# ==============================================================
#  腾讯行情接口（最稳定，任何时候都可用）
# ==============================================================

def _tencent_quote(code):
    """获取腾讯个股/指数行情

    V2.1 S1：真正的请求与解析搬到了 services/data/providers/tencent.py。
    这里保留原函数名与返回结构（~ 切分后的字段列表），失败依旧返回 None ——
    上层 get_stock_quote / get_market_flow 完全不用改。
    需要知道"到底为什么失败"时用 services.data.registry.call() 拿 ProviderResult。
    """
    result = _providers.call("tencent_quote", code)
    return result.data if result.ok() else None


# ==============================================================
#  1. 全市资金走向（腾讯大盘 + 模拟拆分）
# ==============================================================

MARKET_FLOW_INDICES = (
    {"name": "上证指数", "code": "000001"},     # → secid 1.000001（指数语义，不是平安银行）
    {"name": "深证成指", "code": "399001"},     # → secid 0.399001
)


def get_market_flow():
    """全市资金流向（基于东方财富真实数据 + 文件缓存）"""
    def _fetch():
        # 只有真正从行情源拿到时间才填：以前默认 datetime.now()，
        # 源不给时间（或干脆没数据）时会把"今天"当成交易日写进页面，属于凭空生成日期。
        date = ""
        index_name = "上证指数"
        index_price = 0
        index_change = 0

        # 并行调用腾讯（大盘行情） + 东方财富（资金流向）
        # 东财这一段整体交给 Provider：ulist.np 的 diff 是"每指数一条快照"，不是 klines，
        # 解析、元→亿、缺字段语义都在 Provider 里（见 §4.6 设计）。
        parts = None
        flow_res = None
        with ThreadPoolExecutor(max_workers=2) as pool:
            tencent_fut = pool.submit(_tencent_quote, "000001")
            em_fut = pool.submit(_providers.call, "eastmoney_flow",
                                 mode="market_snapshot", indices=MARKET_FLOW_INDICES)
            parts = tencent_fut.result()
            flow_res = em_fut.result()

        if parts:
            index_name = parts[1] if parts[1] else "上证指数"
            index_price = float(parts[3]) if parts[3] else 0
            index_change = float(parts[32]) if len(parts) > 32 and parts[32] else 0
            date = parts[30][:8] if len(parts) > 30 and parts[30] else date

        main_force = 0
        super_large = 0
        large_order = 0
        middle_order = 0
        small_order = 0
        flow_available = False      # 东财是否真的给了资金记录（区分"零值"与"没数据"）
        flow_partial = False        # 中单/小单金额缺失 → 未知而不是 0

        if flow_res is not None and flow_res.ok():
            flow_available = True
            total = flow_res.data["total"]
            main_force = total["main_force"]
            super_large = total["super_large"]
            large_order = total["large_order"]
            middle_order = total["middle_order"]
            small_order = total["small_order"]
            flow_partial = bool(flow_res.data.get("partial"))

        # 只要有一边没拿到，就不能组装成"行情"：
        #   - 没有指数 → 价格是假的 0；
        #   - 没有资金记录（flow_available=False）→ 0.00 亿是"没数据"造出来的，不是真实零值。
        # 两种都返回 None，让页面显示"暂无数据"。真实零值（源确实给了全 0 记录）不受影响。
        if not flow_available or index_price <= 0:
            return None

        # 多源累加后的浮点尾巴统一收口（单位已是亿元，两位小数是接口契约）
        main_force = round(main_force, 2)
        super_large = round(super_large, 2)
        large_order = round(large_order, 2)
        middle_order = round(middle_order, 2) if middle_order is not None else None
        small_order = round(small_order, 2) if small_order is not None else None

        if flow_partial:
            # 未知 ≠ 0：前端 fmt.money(None) 会显示 "--"
            middle_order = None
            small_order = None

        return {
            # 字段口径版本：2 = 中单/小单取 f78/f84、金额一律 元→亿。
            # 旧缓存（v1）里的中单/小单是百分比被当成亿，读到就丢弃重取。
            "_flow_fields_v": 2,
            "_flow_available": flow_available,
            "_flow_partial": flow_partial,
            "main_force": main_force,
            "super_large": super_large,
            "large_order": large_order,
            "middle_order": middle_order,
            "small_order": small_order,
            "date": date,
            "index_name": index_name,
            "index_price": index_price,
            "index_change": index_change
        }

    out = _cached_get("market_flow", _fetch, validate_fn=_has_valid_data)
    # 旧口径缓存（v1）自愈：里面的中单/小单是错的，不能继续展示，重取一次覆盖
    if isinstance(out, dict) and out.get("_flow_fields_v") != 2:
        out = _cached_get("market_flow", _fetch, validate_fn=_has_valid_data, force=True)
    return out


# ==============================================================
#  2. 板块热力图（固定板块 + 模拟涨跌）
# ==============================================================

def _num(v, default=0.0):
    """东财接口的缺值用 "-" / None / "" 表示，统一转 float。

    2026-11：get_sector_valuation 原来对整个 diff 循环套一个 try，
    只要有一行 f23（市净率）返回 "-"，float("-") 就抛异常，
    整批 60 个板块直接掉进模拟兜底——模拟一关就变成整页 500。
    现在逐字段安全转换，一行坏值只影响那一行。
    """
    if v is None or v == "" or v == "-" or v == "--":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# 固定板块列表（A股行业板块）
SECTOR_LIST = [
    "银行", "保险", "证券", "房地产开发", "半导体",
    "软件开发", "通信设备", "汽车整车", "汽车零部件", "锂电池",
    "白酒", "食品饮料", "医药商业", "化学制药", "医疗器械",
    "煤炭开采", "钢铁", "有色金属", "电力", "光伏设备",
    "军工装备", "家电", "纺织服装", "商业百货", "文化传媒",
    "游戏", "航空机场", "物流", "工程建设", "水泥建材"
]

SECTOR_FLOW_TTL = 60          # 板块资金流：分钟级变量，5 页抓取成本高，60s 足够
SECTOR_VALUATION_TTL = 300    # 板块估值：PE/PB 是日频基本面，5 分钟粒度已过剩

# 最近一次 Provider 结果（S3-3A：只记在内存供观察）
_SECTOR_LAST_RESULT = {}

# 旧缓存一次性安全刷新：key -> 上次尝试时刻（进程内；重启后再试一次是期望行为）
_LEGACY_MIGRATION_AT = {}


def _sector_meta_from(res, value, ts, cache_key, mode):
    """把 ProviderResult 的三维度固化成可持久化的旁路元数据（S3-3B）。

    这里只记录**这次抓取的事实**（是否成功、行数 vs total、哪些页失败、
    哪些字段缺失、上游有没有给时间）。不记录"这次请求从哪读的"——
    那是 data_meta(served_from=...) 在读取时补的，属于请求维度，不落盘。
    """
    rows = len(value) if isinstance(value, (list, dict)) else None
    is_mock = isinstance(value, list) and any(
        isinstance(r, dict) and r.get("is_mock") for r in value)
    meta = {
        "schema": _META_SCHEMA,
        "cache_key": cache_key,
        "mode": mode,
        "source": (res.source if res is not None else "") or "eastmoney_sector",
        "fetch_status": "not_attempted",
        "completeness": "unknown",
        "rows": rows,
        "total": None,
        "missing_pages": [],
        "missing": [],
        "notes": [],
        "duplicates": 0,
        "fetched_at": float(ts),
        "asof": "",
        "asof_source": "unknown",
        "is_mock": bool(is_mock),
        "data_hash": _fingerprint(value),
        "written_at": time.time(),
    }
    if is_mock or res is None:
        # 模拟数据永远不标 success/complete：它只是界面演示，不是上游事实
        meta["notes"] = ["mock_fallback"] if is_mock else ["no_provider_result"]
        return meta

    extra = res.extra or {}
    dims = res.dimensions()
    completeness = extra.get("completeness") or dims.get("completeness") or "unknown"
    total = extra.get("total")
    if completeness == "complete" and (total is None or rows != total):
        # 防御：自称完整但行数对不上 total → 降级，绝不冒充完整
        completeness = "partial" if total is not None else "unknown"
    meta.update({
        "fetch_status": dims.get("fetch_status") or "unknown",
        "completeness": completeness,
        "total": total,
        "missing_pages": list(extra.get("missing_pages") or []),
        "missing": [str(m) for m in (extra.get("missing") or [])],
        "notes": [str(n) for n in (extra.get("notes") or [])],
        "duplicates": int(extra.get("duplicates") or 0),
        "asof": res.asof or "",
        "asof_source": "upstream" if res.asof else "unknown",
    })
    return meta


def _needs_legacy_migration(cache_key):
    """旧缓存（无旁路元数据 / 元数据与数据对不上）是否需要一次性安全刷新。

    不用 `len(data) < N` 这种通用阈值 —— 合法的 5 行测试数据不能被当成坏缓存删掉。
    判据只有一条：**有没有与当前数据哈希绑定的可信元数据**。
    刷新失败时调用方仍会退回旧数据，只是如实标 unknown/stale（见 data_meta）。
    """
    data, fetched_at = _cache_payload(cache_key)
    if data is None:
        return False
    # r1：与 data_meta 用**同一套**可信性规则（schema + hash + fetched_at），
    # 不能只比 hash —— 否则 schema 不对/时间对不上的旧元数据会让迁移永远不触发。
    if _meta_trust(_meta_record(cache_key), data, fetched_at) == "ok":
        return False                      # 元数据可信且绑定当前数据 → 不需要迁移
    last = _LEGACY_MIGRATION_AT.get(cache_key)
    if last is not None and (time.time() - last) < LEGACY_MIGRATION_INTERVAL:
        return False                      # 冷却中：避免每个请求都打一遍 5 页上游
    return True


def _mock_sector_flow():
    """模拟板块资金流（默认关闭；仅 ALLOW_MOCK_FALLBACK=True 时用于界面演示）。

    ⚠️ Q7=A：S3-6 连同 ALLOW_MOCK_FALLBACK 一起删除。保留期间必须打 is_mock=True，
       不允许混进真实分析。
    """
    result = []
    market_change = _get_market_change()
    random.seed(datetime.now().strftime("%Y%m%d"))
    growth = ["半导体", "软件开发", "通信设备", "光伏设备", "锂电池", "军工装备", "游戏"]
    cyclical = ["证券", "有色金属", "煤炭开采", "钢铁", "水泥建材", "房地产开发", "汽车整车"]
    defensive = ["银行", "电力", "食品饮料", "煤炭开采", "医药商业", "白酒", "保险"]
    for name in SECTOR_LIST:
        if name in growth:
            offset = random.uniform(-2.0, 4.0)
        elif name in cyclical:
            offset = random.uniform(-3.0, 3.0)
        else:
            offset = random.uniform(-1.5, 2.0)
        change = round(market_change + offset, 2)
        if name in defensive and market_change < -1:
            change = round(change + random.uniform(1.0, 3.0), 2)
            change = min(change, 2.0)
        flow = round(change * random.uniform(2, 6), 2)
        result.append({"name": name, "change": change, "flow": flow})
    result.sort(key=lambda x: x["change"], reverse=True)
    for row in result:
        row["is_mock"] = True
    return result


def _sector_provider_result(mode):
    """调用板块 Provider（registry 里已注册）并留一份结果供观察/后续元数据使用。"""
    res = _providers.call("eastmoney_sector", mode=mode)
    _SECTOR_LAST_RESULT[mode] = res
    return res


def _validate_sectors(data):
    if not data or not isinstance(data, list):
        return False
    # 至少有一条数据有非零的 change 或 flow
    for s in data:
        if not isinstance(s, dict):
            continue
        if abs(s.get("change") or 0) > 0.01 or abs(s.get("flow") or 0) > 0.01:
            return True
    return False


def _sector_cached_get(cache_key, mode, ttl, trace=None):
    """板块两个接口共用的取数路径：Provider → 缓存 → 旁路元数据 → 旧缓存迁移。

    - `meta_fn` 让 metadata 与数据同一次落盘（partial 不会在缓存里变成 complete）
    - 旧缓存（无可信元数据）先做**一次**安全刷新；刷新失败仍返回旧数据，
      由 data_meta() 如实标 unknown/stale（绝不冒充完整）
    """
    captured = {}

    def _fetch():
        res = _sector_provider_result(mode)
        captured["res"] = res
        if res.status in ("failed", "empty") or not res.data:
            if ALLOW_MOCK_FALLBACK:
                return _mock_sector_flow() if mode == "flow" else _mock_sector_valuation()
            return None
        return res.data

    def _meta(value, ts):
        return _sector_meta_from(captured.get("res"), value, ts, cache_key, mode)

    def _finish(out):
        """Provider 内部把异常转成 failed 状态，不会往上抛 ——
        所以"这次到底成没成"要从 ProviderResult 回填到 trace，供 Headers 使用。"""
        if out is None and trace is not None:
            res = captured.get("res")
            if res is not None:
                if res.status == "failed":
                    trace["fetch_failed"] = True
                elif res.status == "empty":
                    trace["fetch_empty"] = True
        return out

    if _needs_legacy_migration(cache_key):
        _LEGACY_MIGRATION_AT[cache_key] = time.time()
        try:
            return _finish(_cached_get(cache_key, _fetch, validate_fn=_validate_sectors,
                                       source="eastmoney_sector", ttl=ttl, force=True,
                                       trace=trace, meta_fn=_meta))
        except Exception:
            # 一次性安全刷新失败：继续走正常路径（有旧缓存就返回旧缓存）
            if trace is not None:
                trace.pop("served", None)
            if captured.get("res") is None:
                captured.clear()
    return _finish(_cached_get(cache_key, _fetch, validate_fn=_validate_sectors,
                               source="eastmoney_sector", ttl=ttl,
                               trace=trace, meta_fn=_meta))


def get_sector_flow(trace=None):
    """行业板块资金流（全量 496：pz=100 × 5 页，按 f12 去重；60s 缓存）。

    与迁移前的区别：
      - 不再只取第 1 页 60 条（TD-17）；
      - 缺失字段给 None，不再用 0 顶替（TD-16 同源）；
      - 单页失败 → Provider 返回 partial + missing_pages，此处保留已抓到的行，
        不会把"部分成功"伪装成完整成功（异常/全失败才降级到 Mock 或 None）。

    S3-3B（旧 60 行缓存根治）：
      升级前写入的 `sector_flow` 缓存只有 60 行且没有旁路元数据。这里**不用行数阈值**
      判断好坏，只看有没有与数据哈希绑定的可信元数据：
        - 有元数据且 rows == total → complete；rows < total → partial；
        - 没有元数据 → unknown，并触发一次安全刷新（冷却 30 分钟）；
        - 刷新失败仍返回旧数据，但元数据保持 unknown/stale，不冒充完整。
      合法的 5 行测试缓存同样不会被删，只会在下一次成功刷新时被真实数据替换。
    """
    return _sector_cached_get("sector_flow", "flow", SECTOR_FLOW_TTL, trace=trace)


def _get_market_change():
    """获取真实大盘涨跌幅作为基准"""
    try:
        resp = requests.get("http://qt.gtimg.cn/q=sh000001",
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        resp.encoding = "gbk"
        parts = resp.text.split("~")
        if len(parts) > 32 and parts[32]:
            return float(parts[32])
    except:
        pass
    return -0.5  # 默认微跌


# ==============================================================
#  3. 个股行情（腾讯，最稳定）
# ==============================================================

def cache_meta(cache_key):
    """给接口层用的缓存元信息：asof 是格式化字符串，is_stale 按交易/休盘分别判定。

    list 型缓存（板块资金流）没法往数据里塞 _asof，只能从 _LAST_SUCCESS 反查。
    """
    ts = _LAST_SUCCESS.get(cache_key)
    status, trading = _market_status()
    if not ts:
        return {"asof": None, "is_stale": True, "age_sec": None,
                "reason": "没有成功的抓取记录", "market_status": status}
    age = time.time() - ts
    limit = STALE_AFTER_SEC if trading else 3 * 86400
    return {
        "asof": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
        "is_stale": age > limit,
        "age_sec": round(age, 1),
        "reason": "" if age <= limit else f"缓存已 {age/60:.0f} 分钟未更新",
        "market_status": status,
    }


# 盘中同一证券 30 秒内只打一次上游（Q5=A：盘中 30s TTL，休市走文件缓存）
QUOTE_CACHE_TTL = 30

# 每只证券最近一次**真实抓取**的结果（与"这次返回的数据来自哪里"是两件事）
_QUOTE_ATTEMPT = {}

# 每只证券最近一次调用**实际返回的数据来源**：network / memory / memory_stale / file / file_fallback / none
_QUOTE_SERVED = {}


def _note_quote_attempt(stock_code, ok, reason=""):
    # 防御：这个字典按证券代码累积，且没有过期概念。正常页面只会涉及几十只，
    # 但万一被脚本扫全市场，这里不能无限长——超过 1024 只就整体清空（只影响可观测性）。
    if len(_QUOTE_ATTEMPT) > 1024:
        _QUOTE_ATTEMPT.clear()
        _QUOTE_SERVED.clear()
    _QUOTE_ATTEMPT[stock_code] = {"ok": bool(ok), "ts": time.time(), "reason": reason}


def _served_from_trace(trace):
    """读取 _cached_get 记录的实际来源；内部用词 stale → 对外 memory_stale。"""
    where = (trace or {}).get("served") or "none"
    return "memory_stale" if where == "stale" else where


def quote_cache_meta(stock_code):
    """行情的新鲜度、来源与失败状态（不改 /api/stock-quote 的 18 字段契约）。

    刻意把两个概念分开，避免"上次抓取成功"被当成"这次仍是实时"：
      - served_from       本次返回的数据来自哪一层：
                          network（本次真抓）/ memory（T+30s 内命中内存）/
                          memory_stale（TTL 过期，SWR 先给旧值）/ file（休市读盘）/
                          file_fallback（抓取失败后退回文件缓存）/ none（无数据）
      - last_fetch_status 最近一次真实抓取的结果：ok / failed / none
      - status            对外三态：live（本次真抓）/ cached（任何缓存来源）/ unavailable（无数据）
    """
    key = f"quote_{stock_code}"
    meta = cache_meta(key)
    served = _QUOTE_SERVED.get(stock_code)
    attempt = _QUOTE_ATTEMPT.get(stock_code)
    served_from = served["from"] if served else "none"
    last_status = "none" if attempt is None else ("ok" if attempt["ok"] else "failed")

    if served_from == "network":
        status = "live"
    elif served_from in ("memory", "memory_stale", "file", "file_fallback"):
        status = "cached"
    else:
        # 本次没有返回任何数据：历史 _LAST_SUCCESS（asof）不能证明"现在有缓存可用"
        status = "unavailable" if last_status == "failed" else "unknown"
    reason = ""
    if last_status == "failed" and attempt:
        reason = attempt["reason"]
    meta.update({
        "status": status,
        "served_from": served_from,
        "last_fetch_status": last_status,
        "reason": reason or meta["reason"],
    })
    return meta


def get_stock_quote(stock_code) -> dict | None:
    """个股实时行情（对外仍是 18 个字段，API 契约不变）

    S3-1 两点变化：
      1. 解析从本函数搬到 TencentQuoteProvider.parse_quote()（业务有效性判断仍留在这里）；
      2. 盘中 30 秒缓存（Q5=A）：同一证券 30 秒内连续请求只打一次上游；
         休市直接用文件缓存（定格快照），不冒充实时；缓存命中不刷新 fetched_at。

    返回的 18 个字段里不带任何元数据 —— 需要"这是不是缓存/多旧"时用 quote_cache_meta()，
    避免为了内部可观测性去改动前端 API 契约。
    """
    key = f"quote_{stock_code}"
    provider = _providers.get("tencent_quote")
    trace: dict = {}                        # _cached_get 写入本次真实来源

    def _fetch():
        parts = _tencent_quote(stock_code)
        if not parts:
            _note_quote_attempt(stock_code, False, "上游无数据")
            return None
        try:
            quote = provider.parse_quote(parts, stock_code)
        except Exception as e:                      # 截断/字段不合法 → 不可用，不缓存
            _note_quote_attempt(stock_code, False, f"{type(e).__name__}: {str(e)[:120]}")
            return None
        # 价格缺失/为 0 不是"有效行情"：宁可接口失败，也别让页面显示 0.00 元
        if quote.get("price") is None or quote["price"] <= 0:
            _note_quote_attempt(stock_code, False, "价格无效（空或 <=0）")
            return None
        _note_quote_attempt(stock_code, True)
        return quote

    data = _cached_get(key, _fetch, source="tencent_quote", ttl=QUOTE_CACHE_TTL, trace=trace)
    _QUOTE_SERVED[stock_code] = {"from": _served_from_trace(trace), "ts": time.time()}

    if not isinstance(data, dict) or not data:
        return None
    # 只投影 18 个契约字段：_asof/_stale/_market_status 等缓存标注不外泄；
    # 空值保持 None（"源没给"≠0），不在这里二次补零
    return {f: data.get(f) for f in provider.QUOTE_FIELDS}


# ==============================================================
#  4. 个股资金流向（东方财富 fflow/daykline 真实数据）
#  2026-11 改造：原实现用 random.seed 按成交额编造一条"看起来像"的曲线，
#  接口又不带任何 mock 标记，等于把模拟数据当真实数据展示。现改为真实逐日资金流。
# ==============================================================

def _fflow_secid(stock_code):
    """东财 secid（兼容保留）：规则已统一到 normalizers.secid()。

    老测试仍在锁这个函数的输出，所以保留薄委托而不是删掉；
    真正的市场判断（含 000001 指数/个股歧义、未知代码报错）在 normalizers.secid()。
    """
    from services.data.normalizers import secid
    return secid(stock_code, "stock")


def get_stock_money_flow(stock_code, days=20):
    """个股近 days 个交易日资金流向（东方财富 fflow/daykline，单位：亿元）

    字段对应（东财返回，单位元）：
      f51 日期 / f52 主力净额 / f53 小单 / f54 中单 / f55 大单 / f56 超大单
    主力 = 大单 + 超大单（已对 13 只自选股逐日核对一致）
    返回 {dates, flows:{super_large,large,middle,small}, unit, is_mock}
    接口失败返回 None → 路由显示"暂无数据"，不再随机兜底
    """
    def fetch():
        # 请求与解析都在 Provider（services/data/providers/eastmoney_flow.py）：
        # klines 列序固定（不跟随 fields2）、元→亿、只取最近 days 行；失败返回 None，不补零不编日期。
        result = _providers.call("eastmoney_flow", mode="history",
                                 code=stock_code, days=days)
        return result.data if result.ok() else None

    return _cached_get(f"stockflow_hist_{stock_code}", fetch,
                       validate_fn=lambda x: bool(x and x.get("dates")),
                       source="eastmoney", ttl=300)


# ==============================================================
#  4b. 个股当日资金净流向 + 占比（东方财富真实数据）
# ==============================================================

def get_stock_real_money_flow(stock_code):
    """个股当日资金净流向及占比（东方财富真实数据）
    返回：{main_force, super_large, large, middle, small} 均为亿元
          {main_force_pct, super_large_pct, large_pct, middle_pct, small_pct} 均为 %
    其中 large_pct 即 大单净量（大单净流入占成交额比例）
    """
    # 成交额（腾讯）仍是占比的分母：本函数口径 = 资金净额 / 成交额
    quote = get_stock_quote(stock_code)
    if not quote:
        return None
    # 成交额缺失 ≠ 0：没有分母就不给比例（None），不再用 max(..., 0.01) 造一个有效百分比。
    # 完整的"停牌 0 成交额"口径统一留给 S3-2，这里只保证空值不冒充 0。
    turnover_yi = quote.get("turnover")

    result = _providers.call("eastmoney_flow", mode="today", code=stock_code)
    if not result.ok():
        return None
    amounts = result.data["amounts_yuan"]      # 单位：元（Provider 不做四舍五入，避免占比失真）

    def yi(v):
        return round(v / 1e8, 2)

    # 分母 = **真实成交额**（亿元）。S3-2：删除 `max(turnover, 0.01)` 的下限钳制。
    #   None / 非数值 / NaN / <=0  → 占比不可计算 → None（页面显示 --）
    #   有效成交额（>0）           → 直接做分母，不抬高也不压低
    #   真实资金净额 0 且分母有效  → 0.0（真实零，不是 None）
    def valid_turnover(t):
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            return None
        if not math.isfinite(t) or t <= 0:
            return None
        return float(t)

    denominator = valid_turnover(turnover_yi)

    def pct(v):
        if denominator is None:
            return None
        return round(v / (denominator * 1e8) * 100, 2)

    return {
        "main_force": yi(amounts["main_force"]),
        "super_large": yi(amounts["super_large"]),
        "large": yi(amounts["large"]),
        "middle": yi(amounts["middle"]),
        "small": yi(amounts["small"]),
        "main_force_pct": pct(amounts["main_force"]),
        "super_large_pct": pct(amounts["super_large"]),
        "large_pct": pct(amounts["large"]),       # 大单净量
        "middle_pct": pct(amounts["middle"]),
        "small_pct": pct(amounts["small"]),
    }


# ==============================================================
#  5. 估值数据（腾讯真实PE + 行业对比）
# ==============================================================

# 行业平均PE对照表（仅作 get_industry_pe 失败时的降级兜底）
INDUSTRY_PE_MAP = {
    "600519": 35, "000858": 30,  # 白酒
    "300750": 40, "002594": 45,  # 新能源
    "600036": 7, "601398": 6, "601166": 5,  # 银行
    "601318": 12, "601628": 15,  # 保险
    "000333": 15, "000651": 12,  # 家电
    "600276": 40, "300760": 45,  # 医药
    "600900": 22,  # 电力
    "300059": 35,  # 券商/金融
    "600887": 25, "600882": 20,  # 食品
    "002415": 25, "000063": 30,  # 通信
}


# ---- S3-4A r3/r4：F10 财报缓存的数值真实性与分层安全 ---------------------
# Provider 只保证"这次网络抓到的"是有限值。历史 _data_cache.json 或进程内缓存里的
# 旧条目不会再经过 normalize()：一份 eps=inf 的旧财报照样会被读出来，估值侧就会得到
# available=True、内在价值 inf、偏离度 nan，严格 JSON 序列化直接抛错。
#
# r4 修正两个边界（GPT 复审）：
#   1. 内存坏 ≠ 文件坏。旧实现"发现坏值就两层全删"，会把一份合法文件缓存一起误删。
#      现在按层判定：哪一层坏了删哪一层，另一层不动。
#   2. 过期内存坏值不能既被当 SWR 旧数据启动后台刷新、又被同步强制重抓。
#      现在进 _cached_get 之前先把明显非法的内存条目摘掉，同一轮只有一次恢复请求。
_F10_NUMERIC_FIELDS = ("eps", "bps", "revenue", "revenue_yoy",
                       "net_profit", "profit_yoy", "roe")
_FIN_LAST_DIAG = {}     # code -> 最近一次 F10 取数状态（仅供排查/S3-4B 设计参考）


def _fin_values_finite(fin):
    """财报数值有效性：None 或有限数值才算有效；bool / str / NaN / inf 一律无效。

    只读不写：不把坏值改成 0，也不删字段 —— 无效就是无效，交给调用方 reject。
    """
    if not isinstance(fin, dict):
        return False
    for field in _F10_NUMERIC_FIELDS:
        value = fin.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if isinstance(value, float) and not math.isfinite(value):
            return False
    return True


def _fin_memory_invalid(stock_code):
    """内存里那条 F10 是否存在且非法（不存在 → False，不构成问题）。"""
    entry = _IN_MEMORY_CACHE.get(f"fin_{stock_code}")
    if not isinstance(entry, dict):
        return False
    return not _fin_values_finite(entry.get("data"))


def _drop_fin_memory(stock_code):
    """只淘汰内存条目（含它自己的 _LAST_SUCCESS 时间）。文件缓存一个字不动。"""
    key = f"fin_{stock_code}"
    with _CACHE_LOCK:
        had = _IN_MEMORY_CACHE.pop(key, None) is not None
        _LAST_SUCCESS.pop(key, None)
    return had


def _drop_fin_file(stock_code):
    """只淘汰 _data_cache.json 里的 fin_{code}（及其旁路元数据）。内存不动。"""
    key = f"fin_{stock_code}"
    with _CACHE_LOCK:
        cache = _load_cache()
        if key not in cache:
            return False
        cache.pop(key, None)
        meta_store = cache.get(_META_NAMESPACE)
        if isinstance(meta_store, dict):
            meta_store.pop(key, None)
            if not meta_store:
                cache.pop(_META_NAMESPACE, None)
        _save_cache(cache)
    return True


def _fin_memory_snapshot(stock_code):
    """取内存里那条**合法**财报快照：(data, ts)；没有或非法则 (None, None)。

    只读不删，也不改时间 —— 用于"文件缓存坏了、上游又不可达"时的降级回退。
    """
    entry = _IN_MEMORY_CACHE.get(f"fin_{stock_code}")
    if not isinstance(entry, dict):
        return None, None
    data = entry.get("data")
    if not _fin_values_finite(data):
        return None, None
    return data, entry.get("time")


def _fin_memory_fallback(stock_code, reason):
    """把合法内存快照包装成"降级数据"返回：保留原始抓取时间，显式标 stale。

    明确不是本次网络成功：`_source` 仍写来源，但 `_stale=True` + 文案说明；
    `_asof` 用内存条目自己的时间，绝不刷成 now。
    """
    data, ts = _fin_memory_snapshot(stock_code)
    if data is None:
        return None
    out = dict(data)
    out["_asof"] = ts
    out["_asof_known"] = ts is not None
    out["_age_sec"] = round(time.time() - ts, 1) if ts else None
    out["_stale"] = True
    out["_stale_reason"] = reason
    out["_market_status"] = _market_status()[0]
    out["_source"] = "eastmoney"
    out["_fallback"] = "memory"
    return out


def get_financials_diagnostic(stock_code):
    """最近一次 F10 取数状态（ok / cache_hit / empty / failed / cache_rejected）。

    ⚠️ 只是排查用的模块级最新值，不具备逐请求绑定：并发或连续调用会被后一次覆盖。
    S3-4B 做页面状态时必须按"本次请求的来源与实际返回值"现场生成，不能直接拿这个当状态源。
    """
    return dict(_FIN_LAST_DIAG.get(stock_code) or {})


_FIN_TRACE_LOCAL = threading.local()


def _fin_sync_trace():
    """当前线程正在为哪个请求做同步取数（没有 → None，例如后台 SWR 线程）。"""
    return getattr(_FIN_TRACE_LOCAL, "trace", None)


def _fin_build_status(trace, fin):
    """把一次请求的 trace 翻译成**请求级**财报状态对象（S3-4B）。

    六个事实分开说，不合并：
      fetch_status  本次有没有打上游、结果如何（success/empty/failed/not_attempted）
      served_from   这份数据从哪来（network/memory/file/file_fallback/memory_fallback/none）
      freshness     相对于数据自身抓取时间的新鲜度（fresh/stale/unknown）
      fetched_at    本次返回数据自己的抓取时间（本地时间字符串，绝不等于 report_date）
      report_date   财报报告期（来自上游 REPORT_DATE）
      cache_rejected 本次是否发生过非法缓存淘汰
    另一个 report 字段 reason 是给人和日志看的原因说明。
    """
    served = trace.get("served") or "none"
    attempts = int(trace.get("attempts") or 0)
    provider = trace.get("provider_status")
    provider_reason = trace.get("provider_reason") or ""
    rejected = bool(trace.get("cache_rejected"))

    served_from = {
        "network": "network",
        "memory": "memory", "stale": "memory",
        "file": "file",
        "file_fallback": "file_fallback",
        "memory_fallback": "memory_fallback",
    }.get(served, "none")

    # 1) 本次有没有请求上游
    if served_from == "network":
        fetch_status = "success"
    elif served_from in ("file_fallback", "memory_fallback"):
        # 回退就是回退，不能写 success；但上游到底是 empty 还是 failed 要如实保留
        fetch_status = provider if provider in ("empty", "failed") else "failed"
    elif attempts == 0:
        fetch_status = "not_attempted"   # 纯缓存命中，不继承上一轮失败
    else:
        fetch_status = provider if provider in ("success", "empty", "failed") else "failed"

    if fin is None:
        if provider in ("empty", "failed"):
            fetch_status = provider
        elif attempts == 0:
            fetch_status = "not_attempted"

    asof = fin.get("_asof") if isinstance(fin, dict) else None
    stale_flag = fin.get("_stale") if isinstance(fin, dict) else None

    # 2) 新鲜度
    if served_from == "network":
        freshness = "fresh"
    elif served_from == "memory":
        freshness = "stale" if served == "stale" else "fresh"
    elif served_from == "memory_fallback":
        freshness = "stale"
    elif served_from in ("file", "file_fallback"):
        if asof is None:
            freshness = "unknown"
        elif stale_flag or served_from == "file_fallback":
            freshness = "stale"
        else:
            freshness = "fresh"
    else:
        freshness = "unknown"

    # 3) 原因
    if trace.get("reason"):
        reason = trace["reason"]
    elif served_from == "network":
        reason = "本次抓取成功"
    elif served_from == "memory":
        reason = ("返回内存中的上一轮财报，后台正在刷新" if served == "stale"
                  else "命中内存缓存，本次未请求上游")
    elif served_from == "file":
        reason = "读取本地财报快照（本进程未请求上游）"
    elif served_from == "file_fallback":
        reason = ("本次上游未返回财报记录，回退到文件缓存（历史快照）"
                  if fetch_status == "empty" else "本次抓取失败，回退到文件缓存")
    elif served_from == "memory_fallback":
        reason = ("本次上游未返回财报记录，回退到内存中的财报快照"
                  if fetch_status == "empty" else "本次抓取失败，回退到内存中的财报快照")
    elif fetch_status == "empty":
        reason = "上游正常响应但没有财报记录"
    elif fetch_status == "failed":
        reason = provider_reason or "本次未取得财报"
    else:
        reason = "本次未请求上游，也没有可用财报"

    return {
        "fetch_status": fetch_status,
        "served_from": served_from,
        "freshness": freshness,
        "fetched_at": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(asof)))
                       if asof else None),
        "report_date": (fin.get("report_date") if isinstance(fin, dict) else None) or None,
        "cache_rejected": rejected,
        "reason": reason,
    }


def _fin_legacy_label(status):
    """旧版排查标签（S3-4A 的 status 字段），只给 _FIN_LAST_DIAG 用。"""
    if status.get("served_from") == "memory_fallback":
        return "memory_fallback"
    fetch = status.get("fetch_status")
    if fetch == "success":
        return "ok" if status.get("served_from") == "network" else "cache_hit"
    if fetch == "empty":
        return "empty"
    if fetch == "failed":
        return "failed"
    if fetch == "not_attempted":
        return "cache_hit" if status.get("served_from") not in ("none",) else "failed"
    return "failed"


def _fin_get_with_cache(stock_code, refresh, trace):
    """真正干活的部分：缓存读取、坏缓存淘汰、有界重抓、合法回退。状态写进 trace。"""
    key = f"fin_{stock_code}"
    trace.setdefault("attempts", 0)
    trace.setdefault("cache_rejected", False)

    def fetch():
        result = _providers.call("eastmoney_f10", stock_code)
        # 只把状态记给"当前线程正在服务的那个同步请求"。后台 SWR 线程在这里拿不到
        # trace（thread-local 为空），因此它抓到的新财报只会走缓存更新，
        # 绝不会把前台这次响应的 fetch_status 改成 success。
        tl = _fin_sync_trace()
        if tl is not None:
            tl["attempts"] = tl.get("attempts", 0) + 1
            tl["provider_status"] = result.status if not result.ok() else "success"
            tl["provider_reason"] = (result.error or {}).get("message", "") if not result.ok() else ""
        return result.data if result.ok() else None

    def read(force=False):
        sub = {}
        prev = getattr(_FIN_TRACE_LOCAL, "trace", None)
        _FIN_TRACE_LOCAL.trace = trace          # 只在本线程、本次同步读期间有效
        try:
            value = _cached_get(key, fetch, validate_fn=_fin_values_finite,
                                source="eastmoney", ttl=6 * 3600, force=force, trace=sub)
        finally:
            _FIN_TRACE_LOCAL.trace = prev
        trace["served"] = sub.get("served")
        return value

    if _fin_memory_invalid(stock_code):
        _drop_fin_memory(stock_code)
        trace["cache_rejected"] = True
        trace["cache_reason"] = "内存缓存中的财报含 NaN/Infinity，已淘汰内存条目"

    fin = read(force=refresh)

    if fin is not None and not _fin_values_finite(fin):
        served = trace.get("served")
        if served in ("memory", "stale"):
            _drop_fin_memory(stock_code)
        else:
            _drop_fin_file(stock_code)
        trace["cache_rejected"] = True
        trace["cache_reason"] = f"缓存中的财报含 NaN/Infinity（来源 {served}），已淘汰该层"
        if served == "file_fallback":
            fb = _fin_memory_fallback(
                stock_code, "本次抓取失败、文件缓存不可用，回退到内存中的财报快照（非本次网络结果）")
            if fb is not None:
                trace["served"] = "memory_fallback"
                trace["reason"] = f"{trace['cache_reason']}；已回退到内存中的合法财报快照"
                return fb
            trace["reason"] = f"{trace['cache_reason']}；本轮上游已失败，不再重试"
            return None
        trace["cache_reason"] = f"{trace['cache_reason']}并重抓一次"
        fin = read(force=True)
        if fin is not None and not _fin_values_finite(fin):
            trace["reason"] = f"{trace['cache_reason']}；重抓后仍含 NaN/Infinity，拒绝返回"
            return None

    if fin is not None:
        if trace.get("cache_reason"):
            trace["reason"] = trace["cache_reason"]
        return fin

    fb = _fin_memory_fallback(
        stock_code, "本次未取得新的财报，回退到内存中的财报快照（非本次网络结果）")
    if fb is not None:
        trace["served"] = "memory_fallback"
        if trace.get("cache_reason"):
            trace["reason"] = f"{trace['cache_reason']}；已回退到内存中的合法财报快照"
        # 没有缓存淘汰时不要在这里写 reason：交给 _fin_build_status 按
        # empty / failed 分别生成（上游没返回财报 ≠ 抓取失败）
        return fb

    if trace.get("cache_reason"):
        trace["reason"] = (f"{trace['cache_reason']}；本次未取得财报"
                           f"（{trace.get('provider_reason') or '上游不可用'}）")
    return None


def get_latest_financials(stock_code, refresh=False, trace=None):
    """最新一期财报（东方财富 F10 主要财务指标）

    返回 {report_date, report_name, eps, bps, revenue, revenue_yoy,
          net_profit, profit_yoy, roe, notice_date} 或 None（没有可用财报）
    2026-08 新增：估值偏离需结合最新季报（净利润/营收同比、EPS）综合判断

    S3-4A：请求、市场后缀、字段解析在 `EastMoneyF10Provider`；这里只保留缓存
    （fin_{code} / 6 小时 TTL / refresh 穿透）与旧返回契约。
    原采集出处（S3-0 夹具 source_ref 指向这里，勿删）：datacenter-web.eastmoney.com
    /api/data/v1/get（RPT_F10_FINANCE_MAINFINADATA）。

    S3-4B：`trace` 是**调用方自己的**字典，本次请求结束后会多出 `trace["status"]`
    （请求级财报状态：fetch_status / served_from / freshness / fetched_at /
    report_date / cache_rejected / reason）。不传 trace 时行为和以前完全一样。
    返回值契约不变：仍是十字段 dict 或 None，状态绝不塞进财报本体。
    """
    trace = {} if trace is None else trace
    fin = None
    try:
        fin = _fin_get_with_cache(stock_code, refresh, trace)
    finally:
        trace["_closed"] = True
        trace["status"] = _fin_build_status(trace, fin)
        try:
            # 仅排查用，别拿去渲染页面：附带旧版 `status` 标签，方便对照历史日志
            _FIN_LAST_DIAG[stock_code] = dict(
                trace["status"], status=_fin_legacy_label(trace["status"]))
        except Exception:
            pass
    return fin


def get_industry_pe(stock_code, refresh=False):
    """动态获取行业平均 PE（东财三步链：个股→行业名 f127→suggest搜BK→板块PE f9×100）

    失败降级到 INDUSTRY_PE_MAP 静态表；**表里没有就是 None**（不再默认 25 倍）。
    返回值带 source/source_label/is_static，页面必须把静态值标成"参考值"。
    """
    def fetch():
        market = "1" if stock_code.startswith(("6", "9")) else "0"
        try:
            # 1) 个股 → 行业名（f127，如"白酒Ⅱ"）
            r = requests.get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={"secid": f"{market}.{stock_code}", "fields": "f57,f127"},
                headers=HEADERS, timeout=6)
            d = r.json().get("data") or {}
            ind_name = d.get("f127")
            if not ind_name:
                return None
            # 2) 行业名 → 板块代码（suggest type=14 板块，取第一个 BK 开头）
            r2 = requests.get(
                "https://searchapi.eastmoney.com/api/suggest/get",
                params={"input": ind_name, "type": "14", "count": "5"},
                headers=HEADERS, timeout=6)
            data = (r2.json().get("QuotationCodeTable") or {}).get("Data") or []
            bk = next((it.get("Code") for it in data if str(it.get("Code") or "").startswith("BK")), None)
            if not bk:
                return None
            # 3) 板块 → 动态 PE（f9，放大100倍）
            r3 = requests.get(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                params={"secids": f"90.{bk}", "fields": "f12,f14,f9"},
                headers=HEADERS, timeout=6)
            diff = (r3.json().get("data") or {}).get("diff") or []
            if not diff:
                return None
            pe = diff[0].get("f9")
            if not pe or pe <= 0:
                return None
            return {"pe": round(pe / 100, 2),
                    "industry": ind_name,
                    "source": "eastmoney",
                    "source_label": "东方财富行业板块动态 PE（实时接口）",
                    "is_static": False}
        except Exception:
            return None

    result = _cached_get(f"industry_pe_{stock_code}", fetch,
                         validate_fn=lambda x: x is not None and x.get("pe"),
                         source="eastmoney", ttl=3600, force=refresh)
    if result and result.get("pe"):
        return result
    # 降级：静态映射表。⚠️ 只对表里有的股票生效；查不到就是 None，
    # 不再默认 25 倍——用"全行业默认倍数"顶替，等于把"不知道"伪装成一个具体估值。
    static_pe = INDUSTRY_PE_MAP.get(stock_code)
    if static_pe:
        return {"pe": static_pe,
                "industry": "未知",
                "source": "static_map",
                "source_label": "内置静态行业 PE（人工估算，非实时，仅供参考）",
                "is_static": True,
                "asof": None}
    return {"pe": None,
            "industry": "未知",
            "source": "unavailable",
            "source_label": "行业 PE 不可用（东财接口失败，且该股票不在静态表内）",
            "is_static": False,
            "asof": None,
            "reason": "没有真实行业 PE，不用默认倍数顶替"}


def get_stock_valuation(stock_code, refresh=False, trace=None):
    """个股估值（refresh=True 时强制穿透行业PE/财报缓存重新拉取）

    2026-11：PE 缺失不再用行业 PE 顶替。
    原实现 pe == 0 时执行 pe = industry_pe，偏离度于是恒等于 0、结论永远"合理"——
    等于把"算不出来"伪装成"估值正常"，亏损股会被判成最健康的那一类。
    现在 PE <= 0 直接给 None + level="数据不可用" + valuation_available=False。
    """
    quote = get_stock_quote(stock_code)
    if not quote:
        return None

    pe = quote.get("pe")
    pb = quote.get("pb")
    ind = get_industry_pe(stock_code, refresh=refresh)
    industry_pe = ind["pe"]
    industry_name = ind.get("industry", "未知")

    pe_missing = pe is None or pe <= 0
    if not pe_missing and industry_pe:
        deviation_pe = round((pe - industry_pe) / industry_pe * 100, 1)
        level = "偏高" if deviation_pe > 20 else ("偏低" if deviation_pe < -20 else "合理")
        reason = ""
        available = True
    else:
        deviation_pe = None
        level = "数据不可用"
        available = False
        reason = ("PE 缺失（亏损股 / 停牌 / 行情源未返回），无法计算估值偏离"
                  if pe_missing else "行业 PE 不可用（未取到实时板块 PE，且该股票不在静态表内），无法计算偏离度")

    result = {
        "pe": round(pe, 2) if not pe_missing else None,
        "pb": round(pb, 2) if pb is not None else None,
        "pe_ttm": round(pe, 2) if not pe_missing else None,
        # 腾讯行情 f39 本身就是市盈率(TTM)，与 pe 同源同口径，这里显式说明，避免被当成两个独立指标
        "pe_ttm_note": "与 PE 同源（腾讯行情 f39 = 市盈率TTM），非独立口径",
        "industry_pe": industry_pe,
        "industry_name": industry_name,
        "industry_pe_source": ind.get("source", "unknown"),
        # 静态表是人工估算、不随时间变化，必须让页面能显示"参考值"标签
        "industry_pe_source_label": ind.get("source_label", ""),
        "industry_pe_is_static": bool(ind.get("is_static")),
        "deviation_pe": deviation_pe,
        "level": level,
        "level_reason": reason,
        "valuation_available": available,
        "date": datetime.now().strftime("%Y-%m-%d"),
    }

    # 2026-08 新增：估值偏离结合最新季报财报 + 产业政策事件
    # 财报（净利润/营收同比、EPS）动态拉取；政策事件手动维护在 policy_notes.py
    # S3-4B：请求级财报状态（本次请求自己的 trace，不读模块级 _FIN_LAST_DIAG）
    fin_trace = {}
    result["financials"] = get_latest_financials(stock_code, refresh=refresh, trace=fin_trace)
    result["financials_status"] = fin_trace.get("status")
    if trace is not None:
        trace["financials_status"] = fin_trace.get("status")
    result["policy_notes"] = get_policy_notes(stock_code, result["industry_name"])
    return result


# ==============================================================
#  6. 搜索股票（东方财富）
# ==============================================================

def search_stock(keyword):
    """搜索股票（全市场: A股/港股/美股/指数/基金）
    返回 [{code, name, market, tag}, ...], A股优先排序
    market: sh/sz/bj/hk/us/idx/fund, 用于后续 K线/行情接口的前缀
    """
    url = "https://searchadapter.eastmoney.com/api/suggest/get"
    params = {
        "input": keyword, "type": 14,
        "token": "D43BF7DAA5C9D33F9AB8D13D8F05E26E8",
        "count": 15
    }
    text = _get(url, params)
    if not text:
        return None
    try:
        data = json.loads(text)
        result = []
        seen = set()
        if data.get("QuotationCodeTable") and data["QuotationCodeTable"].get("Data"):
            for item in data["QuotationCodeTable"]["Data"]:
                code = item.get("Code", "")
                name = item.get("Name", "")
                classify = item.get("Classify", "")
                mkt_num = item.get("MktNum")
                if not code or not name:
                    continue
                market, tag = _classify_market(classify, code, mkt_num)
                if market is None or code in seen:
                    continue
                seen.add(code)
                result.append({"code": code, "name": name, "market": market, "tag": tag})
        if not result:
            return None
        # A股优先, 其余按 港股 > 指数 > 基金 > 美股 排序
        order = {"sh": 0, "sz": 0, "bj": 0, "hk": 1, "idx": 2, "fund": 3, "us": 4}
        result.sort(key=lambda x: order.get(x["market"], 9))
        return result[:12]
    except:
        return None


def _classify_market(classify, code, mkt_num):
    """根据东方财富 suggest 接口的 Classify/MktNum 判断市场
    返回 (market前缀, 显示标签); 不支持的品种返回 (None, None)
    """
    if classify == "AStock":
        if code.startswith(("4", "8", "92")):
            return "bj", "北交所"
        if code.startswith("6"):
            return "sh", "A股"
        return "sz", "A股"
    if classify == "HK":
        return "hk", "港股"
    if classify == "Index":
        if str(mkt_num) == "0":
            return "sz", "指数"
        return "sh", "指数"
    if classify == "Fund":
        if code.startswith(("5", "6")):
            return "sh", "基金"
        return "sz", "基金"
    if classify == "UsStock":
        return "us", "美股"
    # 北证50等特殊指数
    if code.startswith("899"):
        return "bj", "指数"
    # 其余(BK板块/KRX韩国/OTCFUND场外基金/UniversalIndex全球指数等)不支持
    return None, None


# ==============================================================
#  7. 指数历史K线（腾讯 - 用于EMA/斐波那契计算）
# ==============================================================

def get_index_history(index_code, days=30):
    """获取指数历史K线"""
    market = "sh" if index_code.startswith("0") or index_code.startswith("6") else "sz"
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    
    url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    try:
        resp = requests.get(url, params={"param": f"{market}{index_code},day,{start},{end},{days},qfq"},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = resp.json()
        if not data.get("data"): return _mock_index_history(index_code, days) if ALLOW_MOCK_FALLBACK else None
        
        klines = (data["data"].get(f"{market}{index_code}", {}).get("day", []) or
                  data["data"].get(f"{market}{index_code}", {}).get("qfqday", []))
        if not klines or len(klines) < 5: return _mock_index_history(index_code, days) if ALLOW_MOCK_FALLBACK else None
        
        return [{"date": k[0], "open": round(float(k[1]), 2), "close": round(float(k[2]), 2),
                 "high": round(float(k[3]), 2), "low": round(float(k[4]), 2),
                 "volume": int(float(k[5])) if len(k) > 5 else 0} for k in klines]
    except:
        return _mock_index_history(index_code, days) if ALLOW_MOCK_FALLBACK else None


def _mock_index_history(index_code, days=30):
    """模拟历史K线兜底"""
    market = "sh" if index_code.startswith("0") or index_code.startswith("6") else "sz"
    try:
        r = requests.get(f"http://qt.gtimg.cn/q={market}{index_code}",
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        r.encoding = "gbk"
        parts = r.text.split("~")
        cur_price = float(parts[3]) if len(parts) > 3 and parts[3] else 2000
    except:
        cur_price = 2000
    
    random.seed(index_code)
    result = []
    today = datetime.now()
    price = cur_price * 0.85
    
    for i in range(days):
        day = today - timedelta(days=(days - 1 - i))
        while day.weekday() >= 5: day -= timedelta(days=1)
        change = random.uniform(-0.03, 0.03)
        price *= (1 + change)
        result.append({"date": day.strftime("%Y-%m-%d"),
                      "open": round(price * (1 - change * 0.3), 2),
                      "close": round(price, 2),
                      "high": round(price * (1 + abs(random.uniform(0, 0.02))), 2),
                      "low": round(price * (1 - abs(random.uniform(0, 0.02))), 2),
                      "volume": random.randint(5000000, 30000000),
                      "is_mock": True})      # 模拟 K 线，禁止当真实行情用
    return result



def estimate_intrinsic_value(stock_quote, trace=None):
    """估算内在价值（PE 估算法）

    内在价值 = 年化 EPS × 行业 PE × VALUATION_SAFETY_MARGIN
    ├ EPS：只认「最新财报 EPS 按报告期年化」（年报×1 / 三季报×4/3 / 中报×2 / 一季报×4）
    └ 行业 PE：东财动态板块 PE；失败退静态表；都没有 → 不算

    2026-11 整改（对应外部评审 P1-4）：
    1. 0.8 从魔法数字改成 config.VALUATION_SAFETY_MARGIN，并在 model_params 里回显
    2. price/PE 反推的 EPS 不再参与内在价值计算——那是用股价自证合理；
       改成一个独立的 pe_reference（相对 PE 参考估值），明确标注不是财报口径
    3. 行业 PE 拿不到就 available=False，不再有"默认 25 倍"
    """
    pe = stock_quote.get("pe")
    price = stock_quote.get("price")
    code = stock_quote.get("code", "")

    ind = get_industry_pe(code)
    industry_pe = ind["pe"]
    industry_name = ind.get("industry", "未知")

    # EPS：只认最新财报（按报告期年化）；亏损（EPS<=0）不适用 PE 估算法
    eps, eps_source = None, "report_missing"
    raw_eps, report_date, annual_factor = None, "", None
    fin_trace = {}
    fin = get_latest_financials(code, trace=fin_trace)
    fin_status = fin_trace.get("status")
    if trace is not None:
        trace["financials_status"] = fin_status
    if fin and fin.get("eps") not in (None, ""):
        try:
            raw_eps = float(fin["eps"])
            report_date = str(fin.get("report_date") or "")
            if raw_eps > 0:
                month = int((report_date or "12")[5:7])
                annual_factor = {3: 4, 6: 2, 9: 4 / 3, 12: 1}.get(month, 1)
                eps = round(raw_eps * annual_factor, 3)
                eps_source = "quarterly_report"
            else:
                eps_source = "report_nonpositive"
        except (TypeError, ValueError):
            eps, eps_source = None, "report_unparsable"

    model_params = {
        "formula": "年化EPS × 行业PE × safety_margin",
        "safety_margin": VALUATION_SAFETY_MARGIN,
        "eps_source_rule": "仅财报 EPS，按报告期年化；不使用 price/PE 反推值",
        # 透明化（P1-4）：财报期、原始 EPS、年化系数都回显，页面能核对
        "report_date": report_date or None,
        "raw_eps": raw_eps,
        "annual_factor": annual_factor,
        "ttm_used": False,
        "basis": ("EPS 取 %s 报告期原始值 %s，按 %s 年化；TTM 未使用"
                  % (report_date or "未知", raw_eps, annual_factor)
                  if report_date else "无可用财报期，EPS 未参与计算"),
    }

    # 相对 PE 参考：个股 PE 回归行业 PE 时的理论价，用途是"横向比较"，不是内在价值
    pe_reference = None
    if pe is not None and pe > 0 and price and industry_pe:
        pe_reference = {
            "available": True,
            "value": round(price * industry_pe / pe, 2),
            "eps_derived": round(price / pe, 3),
            "label": "相对PE参考（假设个股PE回归行业PE，非财报口径）",
            "reference_only": True,
            "industry_pe": industry_pe,
            "industry_pe_source": ind.get("source", "unknown"),
            "industry_pe_is_static": bool(ind.get("is_static")),
        }

    if not eps or not industry_pe:
        if eps_source == "report_nonpositive":
            reason = "最新财报 EPS ≤ 0（亏损），PE 估算法不适用"
        elif eps_source == "report_unparsable":
            reason = "财报 EPS 字段无法解析"
        elif eps is None:
            reason = "缺少真实财报 EPS（没有季报数据），不以内生价值口径估算（不再用股价/PE 反推）"
        else:
            reason = "行业 PE 不可用（实时板块 PE 未取到，且该股票不在静态表内）"
        return {
            "price": price, "intrinsic_value": None, "gap": None,
            "available": False, "reason": reason,
            "pe": round(pe, 2) if pe is not None and pe > 0 else None,
            "industry_pe": industry_pe, "industry_name": industry_name,
            "industry_pe_source": ind.get("source", "unknown"),
            "industry_pe_is_static": bool(ind.get("is_static")),
            "eps": None, "eps_source": eps_source,
            "pe_reference": pe_reference,
            "model_params": model_params,
            "financials": fin,
            "financials_status": fin_status,
        }

    intrinsic = round(eps * industry_pe * VALUATION_SAFETY_MARGIN, 2)
    return {
        "price": price, "intrinsic_value": intrinsic,
        "gap": round((price - intrinsic) / intrinsic * 100, 1) if intrinsic else None,
        "available": True, "reason": "",
        "pe": round(pe, 2) if pe is not None and pe > 0 else None,
        "industry_pe": industry_pe,
        "industry_name": industry_name,
        "industry_pe_source": ind.get("source", "unknown"),
        "industry_pe_is_static": bool(ind.get("is_static")),
        "eps": eps, "eps_source": eps_source,
        "pe_reference": pe_reference,
        "model_params": model_params,
        "financials": fin,
        "financials_status": fin_status,
    }


# ==============================================================
#  9. 板块估值
# ==============================================================

def get_sector_valuation(trace=None):
    """各行业板块估值（PE/PB/市值）。

    与迁移前的区别（S3-3 D3/D4）：
      - 不再 `if pe > 0` 丢掉亏损板块：496 个全返回，负 PE 保留原值；
      - `f23(PB)` 实测 496/496 为 "-" → `pb=None`（**不是 0.0**）；
      - 不可解析的字段抛格式错，不再 `except:` 吞成 Mock/None；
      - 带 300s 缓存，避免每次刷新都打 5 页。

    S3-3B：PB 全为 null 是上游真实口径（只记 notes=pb_unavailable:N），
    496 行对齐 total 时完整性仍然是 complete —— 不能因为字段缺失就把整份数据判成 partial。
    """
    return _sector_cached_get("sector_valuation", "valuation", SECTOR_VALUATION_TTL,
                              trace=trace)



def _mock_sector_valuation():
    """模拟板块估值"""
    sectors = [("白酒",35,8),("银行",6,0.7),("保险",12,1.5),("证券",22,1.8),
               ("半导体",60,5),("软件开发",55,4.5),("医药生物",40,4),("新能源",42,3.5),
               ("光伏",35,3),("汽车整车",28,2.5),("家电",15,2.8),("食品饮料",30,6),
               ("煤炭",8,1.2),("有色金属",18,2.5),("电力",22,1.8),("军工",55,3.5),
               ("通信",32,2.8),("房地产",10,0.8),("化工",20,2.2),("机械设备",25,2.5)]
    random.seed(datetime.now().strftime("%Y%m"))
    return [{"name": n, "pe": round(p * random.uniform(0.7,1.3), 2),
             "pb": round(q * random.uniform(0.8,1.2), 2),
             "change": round(random.uniform(-3,3), 2),
             "market_cap": round(random.uniform(500,30000), 2),
             "is_mock": True} for n, p, q in sectors]   # 模拟估值，前端必须标"模拟数据"
