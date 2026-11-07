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
    status, why = _age_status(v.get("at"))
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
    if not stats:
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
    return _row("外部数据源", status, None, "", detail, ok=(status == REAL))


def snapshot():
    """返回 {rows, summary, generated_at}；不联网、不写库。"""
    entries = _entries()
    rows = [
        _cache_row("市场资金", entries, ("market_flow",), "market_flow"),
        _cache_row("板块资金", entries, ("sector_flow",), "sector_flow"),
        _cache_row("个股历史资金", entries, ("stockflow_hist_",), "个股历史资金"),
        _cache_row("财报数据", entries, ("fin_",), "财报"),
        _cache_row("行业估值", entries, ("industry_pe_",), "行业 PE"),
        _policy_row(),
        _ai_row(),
        _sources_row(),
    ]

    real_entries = [(k, v) for k, v in entries.items() if not _is_test_key(k)]
    stale = sum(1 for _k, v in real_entries if _age_status(v.get("at"))[0] == STALE)
    oldest = min((float(v.get("at")) for _k, v in real_entries if v.get("at")), default=None)
    rows.append(_row(
        "缓存系统", (REAL if not stale else PARTIAL) if real_entries else UNAVAILABLE,
        _ts(oldest) if oldest else None, "文件缓存",
        "条目 %d 个，过期 %d 个，最老一份 %s" % (
            len(real_entries), stale,
            _dur(time.time() - oldest) if oldest else "无"),
        ok=bool(real_entries)))

    bad = sum(1 for r in rows if not r["ok"])
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rows": rows,
        "summary": {"total": len(rows), "abnormal": bad,
                    "level": "ok" if bad == 0 else ("warn" if bad <= 3 else "bad")},
        "legend": {"REAL": "真实数据（15 分钟内）", "CACHED": "缓存数据（1 天内）",
                   "STALE": "过期或时间未知", "PARTIAL": "部分可用",
                   "UNAVAILABLE": "不可用"},
    }
