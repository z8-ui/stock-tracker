"""数据健康快照（P1-3）

原则（审查 3.3）：
  1. 不发起任何外部请求 —— 只读已有落盘缓存、SQLite 事件库、进程内统计；
  2. 状态只用真实来源和时间判断，绝不把缓存伪装成实时；
  3. 单个数据源失败只标它自己，不等于整个系统失败。

状态口径（3.2）：
  REAL        真实数据，15 分钟内取到
  CACHED      缓存数据，1 天内取到（可用但不是最新）
  STALE       过期（> 1 天，或没有抓取时间无法确认新鲜度）
  PARTIAL     部分可用（同模块里有的有有的没有）
  UNAVAILABLE 完全不可用（没数据 / 读取失败）
"""
import json
import os
import time

import data_service
import events_store

REAL = "REAL"
CACHED = "CACHED"
STALE = "STALE"
PARTIAL = "PARTIAL"
UNAVAILABLE = "UNAVAILABLE"

FRESH_SEC = 15 * 60
CACHED_SEC = 86400

# 单元测试往 _data_cache.json 里写过的键，不能算进生产健康度
_TEST_KEY_PREFIX = ("test_", "swr_", "sf_", "k", "same")


def _is_test_key(k):
    return str(k).split("_")[0] in ("test", "swr", "sf") or str(k) in ("k", "same")


def _session():
    """当前交易状态（'周末休市' / '交易中' / ...）与是否盘中。取不到就返回空。"""
    try:
        status, is_trading = data_service._market_status()
        return str(status or ""), bool(is_trading)
    except Exception:
        return "", False


def _last_trading_day(today=None, lookback=20):
    """最近一个交易日（YYYYMMDD）。休市期间用它判断"这份数据是不是最近交易日的"。

    只看日历，不猜数据：今天就是交易日则返回今天，否则往回找。
    """
    from datetime import date, timedelta
    from market_calendar import load_calendar, is_trading_day
    try:
        cal = load_calendar()
        if today is None:
            today = date.today()
        for i in range(0, lookback + 1):
            d = today - timedelta(days=i)
            ok, _why = is_trading_day(d, cal)
            if ok:
                return d.strftime("%Y%m%d")
    except Exception:
        pass
    return ""


def _fmt_date8(v):
    """20261106 → 2026-11-06（只格式化，不改数据）。"""
    s = str(v or "")
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    return s


def _entries():
    path = getattr(data_service, "_CACHE_FILE", "")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(v, dict) and "__data" in v:
            out[k] = {"data": v.get("__data"), "at": v.get("fetched_at")}
        else:
            out[k] = {"data": v, "at": None}
    return out


def _age_status(at):
    if not at:
        return STALE, "没有抓取时间，无法确认新鲜度"
    age = time.time() - float(at)
    if age <= FRESH_SEC:
        return REAL, ""
    if age <= CACHED_SEC:
        return CACHED, "来自缓存（%s 前）" % _dur(age)
    return STALE, "已 %s 未更新" % _dur(age)


def _closed_recent(at, expected):
    """休市时判断"这份缓存是不是最近交易日之后抓的"。

    是 → 属于正常的周末不更新；否 → 真的过期（例如停在周三）。
    只比日期，不比"现在"，避免把休市时长算成故障时长。
    """
    if not at or not expected:
        return False
    try:
        return time.strftime("%Y%m%d", time.localtime(float(at))) >= str(expected)
    except Exception:
        return False


def _entry_status(at):
    """缓存条目的状态：交易时段按年龄；休市时段按"是否最近交易日之后抓的"。"""
    status, why = _age_status(at)
    session, is_trading = _session()
    if not is_trading and session and status == STALE and _closed_recent(at, _last_trading_day()):
        return CACHED, "%s：数据停在最近交易日，属正常不更新（不是故障）" % session
    return status, why


def _dur(sec):
    sec = int(max(0, sec))
    if sec < 3600:
        return "%d 分钟" % (sec // 60)
    if sec < 86400:
        return "%.1f 小时" % (sec / 3600)
    return "%.1f 天" % (sec / 86400)


def _ts(v):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(v)))
    except Exception:
        return None


def _source_of(data):
    if isinstance(data, dict):
        return str(data.get("_source") or data.get("source") or "")[:40]
    return ""


def _row(module, status, updated_at=None, source="", detail="", ok=None):
    return {
        "module": module,
        "status": status,
        "updated_at": updated_at,
        "source": source or "",
        "detail": detail,
        "ok": (status in (REAL, CACHED)) if ok is None else ok,
    }


def _cache_row(module, entries, prefixes, label):
    hits = [(k, v) for k, v in entries.items()
            if k.startswith(tuple(prefixes)) and not _is_test_key(k)]
    if not hits:
        return _row(module, UNAVAILABLE, None, "", "%s：无缓存记录（本进程还没抓过）" % label)
    hits.sort(key=lambda kv: float(kv[1].get("at") or 0), reverse=True)
    k, v = hits[0]
    status, why = _entry_status(v.get("at"))
    data = v.get("data")
    bits = ["%s %d 条" % (label, len(hits))]
    if isinstance(data, dict):
        if data.get("date"):
            bits.append("数据日期 %s" % data.get("date"))
        if data.get("report_date") or data.get("report"):
            bits.append("报告期 %s" % (data.get("report_date") or data.get("report")))
        if data.get("industry"):
            bits.append("行业 %s" % data.get("industry"))
    if why:
        bits.append(why)
    return _row(module, status, _ts(v.get("at")), _source_of(data), "；".join(bits))


def _market_row(entries):
    """市场资金：休市时展示最近交易日数据 ≠ 过期；数据日期落后于最近交易日才是真过期。"""
    hits = [(k, v) for k, v in entries.items()
            if k.startswith("market_flow") and not _is_test_key(k)]
    session, is_trading = _session()
    if not hits:
        if not is_trading:
            return _row("市场资金", UNAVAILABLE, None, "",
                        "%s：无可用缓存（本进程还没拿到最近交易日数据）" % (session or "休市"),
                        ok=False)
        return _row("市场资金", UNAVAILABLE, None, "", "market_flow：无缓存记录", ok=False)

    hits.sort(key=lambda kv: float(kv[1].get("at") or 0), reverse=True)
    _k, v = hits[0]
    data = v.get("data") or {}
    at = v.get("at")
    data_date = str((data or {}).get("date") or "") if isinstance(data, dict) else ""
    expected = _last_trading_day()
    status, why = _age_status(at)

    bits = ["market_flow"]
    if data_date:
        bits.append("数据日期 %s" % _fmt_date8(data_date))
    if at:
        bits.append("缓存于 %s（%s 前）" % (_ts(at), _dur(time.time() - float(at))))

    if not is_trading:
        # 休市：不刷新是正常的，不能当成故障；但也不能把旧数据说成实时
        if data_date and expected and data_date == expected:
            status, why = CACHED, ""
            bits.append("%s，展示最近交易日收盘数据（不是实时行情）" % (session or "休市"))
        elif data_date and expected:
            status = STALE
            bits.append("%s，但数据停在 %s，最近交易日是 %s" %
                        (session or "休市", _fmt_date8(data_date), _fmt_date8(expected)))
        elif not data_date:
            status = STALE
            bits.append("%s，数据没有日期，无法确认是不是最近交易日的" % (session or "休市"))
    elif why:
        bits.append(why)

    detail = "；".join(bits)
    if isinstance(data, dict) and data.get("_flow_available") is False:
        detail += "；资金明细缺失（指数可用）"
    return _row("市场资金", status, _ts(at), _source_of(data), detail)


def _policy_row():
    try:
        conn = events_store.connect()
        try:
            events_store.init_db(conn)
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            row = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN COALESCE(review_status,'pending')='pending' "
                "THEN 1 ELSE 0 END),0) FROM events").fetchone()
            pending = int(row[0] or 0) if row else 0
            last = conn.execute(
                "SELECT MAX(COALESCE(collected_at, created_at)) FROM events").fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        return _row("政策事件", UNAVAILABLE, None, "sqlite",
                    "事件库读取失败：%s" % type(e).__name__, ok=False)
    if not total:
        return _row("政策事件", UNAVAILABLE, None, "sqlite",
                    "事件库为空（尚未采集），不等于没有政策", ok=False)
    # 待审核是正常流程状态，不是"系统降级"，所以状态只按"库里有数据"给 REAL
    return _row("政策事件", REAL, _ts(last) if last else None, "sqlite",
                "共 %d 条，待审核 %d 条（未审核的不会出现在个股页的政策区块）" % (total, pending))


def _ai_row():
    model, base = "", ""
    try:
        import ai_analysis
        model = str(getattr(ai_analysis, "DEEPSEEK_MODEL", "") or "")
        base = str(getattr(ai_analysis, "DEEPSEEK_BASE", "") or "")
    except Exception:
        model = os.environ.get("DEEPSEEK_MODEL", "")
    if not model:
        return _row("AI 分析", UNAVAILABLE, None, "", "未配置模型名", ok=False)
    return _row("AI 分析", UNAVAILABLE, None, model,
                "模型 %s 已配置%s；本进程暂无分析记录（没调用 ≠ 调用失败）" %
                (model, "（%s）" % base if base else ""), ok=False)


def _sources_row():
    stats = data_service.source_stats() if hasattr(data_service, "source_stats") else {}
    prov = data_service.provider_stats() if hasattr(data_service, "provider_stats") else {}
    session, is_trading = _session()
    if not stats and not prov:
        if not is_trading:
            # 休市不抓取是设计，不是失败：既不记成功也不记失败
            return _row("外部数据源", CACHED, None, "",
                        "%s：休市未发起外部请求（不计为失败，也不冒充成功）" % (session or "休市"),
                        ok=True)
        return _row("外部数据源", UNAVAILABLE, None, "",
                    "本进程尚未调用外部数据源（重启后计数清零）", ok=False)
    worst, worst_rate, total_calls = "", 1.0, 0
    for name, st in stats.items():
        total_calls += st["calls"]
        if st["success_rate"] < worst_rate:
            worst, worst_rate = name, st["success_rate"]
    errs = [n for n, s in stats.items() if s.get("last_error")]
    if worst_rate >= 0.99 and not errs:
        status = REAL
    elif worst_rate >= 0.5:
        status = PARTIAL
    else:
        status = STALE
    detail = "调用 %d 次；最差 %s 成功率 %.0f%%" % (total_calls, worst or "-", worst_rate * 100)
    if errs:
        detail += "；最近错误：%s" % "；".join(
            "%s=%s" % (n, stats[n]["last_error"][:60]) for n in errs[:2])
    if prov:
        # Provider 维度：真实上游请求（provider 名 + 状态分布），与缓存键维度分开列
        pcalls = sum(p["calls"] for p in prov.values())
        parts = ["%s %d 次(成功 %d/空 %d/失败 %d)" %
                 (n, p["calls"], p["ok"], p["empty"], p["failed"]) for n, p in prov.items()]
        detail += "｜上游请求 %d 次：%s" % (pcalls, "；".join(parts))
        bad = [n for n, p in prov.items() if p["failed"]]
        if bad and status == REAL:
            status = PARTIAL
    return _row("外部数据源", status, None, "", detail, ok=(status == REAL))


def _cache_stats_row():
    """缓存命中独立成行：命中不是一次真实请求，必须和上游成功率分开看。"""
    try:
        cs = data_service.cache_stats()
    except Exception:
        return _row("缓存命中", UNAVAILABLE, None, "", "缓存统计不可用", ok=False)
    if not cs.get("total"):
        return None     # 没有任何读写：不是异常，也不该占一行红字
    # 只有休市直出、没有任何未命中 → 缓存健康；未命中多才是 PARTIAL
    status = REAL if (cs.get("miss", 0) == 0 or cs.get("hit_rate", 0) >= 0.5) else PARTIAL
    detail = ("命中 %d / 未命中 %d / 休市直出 %d / SWR 刷新 %d（命中率 %.0f%%）；"
              "命中不算一次外部请求" % (cs.get("hit", 0), cs.get("miss", 0),
                                        cs.get("closed_served", 0), cs.get("refresh", 0),
                                        (cs.get("hit_rate") or 0) * 100))
    return _row("缓存命中", status, None, "文件缓存", detail, ok=True)


def snapshot():
    """返回 {rows, summary, generated_at}；不联网、不写库。"""
    entries = _entries()
    rows = [
        _market_row(entries),
        _cache_row("板块资金", entries, ("sector_flow",), "sector_flow"),
        _cache_row("个股历史资金", entries, ("stockflow_hist_",), "个股历史资金"),
        _cache_row("财报数据", entries, ("fin_",), "财报"),
        _cache_row("行业估值", entries, ("industry_pe_",), "行业 PE"),
        _policy_row(),
        _ai_row(),
        _sources_row(),
        _cache_stats_row(),
    ]
    rows = [r for r in rows if r is not None]

    real_entries = [(k, v) for k, v in entries.items() if not _is_test_key(k)]
    stale = sum(1 for _k, v in real_entries if _entry_status(v.get("at"))[0] == STALE)
    oldest = min((float(v.get("at")) for _k, v in real_entries if v.get("at")), default=None)
    rows.append(_row(
        "缓存系统", (REAL if not stale else PARTIAL) if real_entries else UNAVAILABLE,
        _ts(oldest) if oldest else None, "文件缓存",
        "条目 %d 个，过期 %d 个，最老一份 %s" % (
            len(real_entries), stale,
            _dur(time.time() - oldest) if oldest else "无"),
        ok=bool(real_entries)))

    bad = sum(1 for r in rows if not r["ok"])
    session, is_trading = _session()
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "market": {
            "status": session,
            "is_trading": is_trading,
            "last_trading_day": _fmt_date8(_last_trading_day()),
            "closed": (not is_trading) and bool(session),
        },
        "rows": rows,
        "summary": {"total": len(rows), "abnormal": bad,
                    "level": "ok" if bad == 0 else ("warn" if bad <= 3 else "bad")},
        "legend": {"REAL": "真实数据（15 分钟内）", "CACHED": "缓存数据（1 天内）",
                   "STALE": "过期或时间未知", "PARTIAL": "部分可用",
                   "UNAVAILABLE": "不可用"},
    }
