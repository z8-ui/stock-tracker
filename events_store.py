# -*- coding: utf-8 -*-
"""政策事件中心（MVP）

为什么要有这个文件
------------------
policy_notes.py 是硬编码字典：加一条事件要改代码 + 重启服务，没有来源 URL、没有状态、
同一条"美国禁止废钨出口"要在 4 只钨股里各抄一份文案。

这里用 SQLite 两张表把「事件」和「事件→标的」拆开：

    events        事件本体：日期 / 标题 / 影响说明 / 方向 / 来源 / URL / 指纹 / 状态
    event_links   关联：target_type = stock | industry，target_key = 股票代码或行业名

一条事件挂多个标的只存一份文案，改一次全站生效。

读写规则
--------
- 读：policy_notes.get_policy_notes 先问这里；DB 不存在/为空/出错时回退静态字典，
      页面不会因为 DB 挂了变空白。
- 写：scripts/events_cli.py
      seed   把 policy_notes.py 的老字典导入（幂等）
      add    手动录入
      add-text  粘一段新闻 → DeepSeek 抽成结构化 JSON → 预览 → --yes 才写库
- 去重：fingerprint = sha1(date|title) 前 16 位；同一事件换个标的重录不会多一行。

库文件：data/events.db（可用环境变量 STOCK_EVENTS_DB 覆盖，测试用）
"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get("STOCK_EVENTS_DB") or os.path.join(_ROOT, "data", "events.db")

DIRECTIONS = ("利好", "利空", "中性")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date  TEXT NOT NULL,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    direction   TEXT NOT NULL DEFAULT '中性',
    source      TEXT NOT NULL DEFAULT '',
    source_url  TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_key  TEXT NOT NULL,
    UNIQUE(event_id, target_type, target_key)
);
CREATE INDEX IF NOT EXISTS idx_events_date  ON events(event_date DESC);
CREATE INDEX IF NOT EXISTS idx_links_target ON event_links(target_type, target_key);
"""


# ---------------------------------------------------------------- 基础

def db_path():
    return os.environ.get("STOCK_EVENTS_DB") or DEFAULT_DB


def connect(path=None):
    p = path or db_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn=None):
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    conn.commit()
    if own:
        conn.close()


def _fingerprint(event_date, title):
    raw = f"{(event_date or '').strip()}|{(title or '').strip()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _direction(value):
    v = (value or "").strip()
    return v if v in DIRECTIONS else "中性"


def _norm_targets(stocks=(), industries=()):
    """统一成 [(type, key)]，去掉空值和重复。"""
    out = []
    for code in stocks or ():
        code = str(code).strip()
        if code:
            out.append(("stock", code))
    for name in industries or ():
        name = str(name).strip()
        if name:
            out.append(("industry", name))
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- 写

def add_event(event_date, title, summary="", direction="中性", source="",
              source_url="", stocks=(), industries=(), status="active",
              conn=None):
    """新增一条事件并挂标的；已存在（同日期同标题）则只补挂缺失的标的。

    返回 (event_id, created: bool, new_links: int)
    """
    event_date = (event_date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    title = (title or "").strip()
    if not title:
        raise ValueError("title 不能为空")

    own = conn is None
    conn = conn or connect()
    init_db(conn)
    fp = _fingerprint(event_date, title)
    cur = conn.execute("SELECT id FROM events WHERE fingerprint = ?", (fp,))
    row = cur.fetchone()
    created = row is None
    if created:
        cur = conn.execute(
            "INSERT INTO events (event_date,title,summary,direction,source,source_url,"
            "fingerprint,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (event_date, title, summary or "", _direction(direction), source or "",
             source_url or "", fp, status, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        event_id = cur.lastrowid
    else:
        event_id = row["id"]

    new_links = 0
    for ttype, tkey in _norm_targets(stocks, industries):
        try:
            conn.execute("INSERT INTO event_links (event_id,target_type,target_key) VALUES (?,?,?)",
                         (event_id, ttype, tkey))
            new_links += 1
        except sqlite3.IntegrityError:
            pass    # 已挂过
    conn.commit()
    if own:
        conn.close()
    return event_id, created, new_links


def seed_from_static(conn=None, verbose=False):
    """把 policy_notes.py 里的老字典导入 DB（幂等，可重复跑）。"""
    from policy_notes import POLICY_NOTES, INDUSTRY_POLICY   # 延迟导入，避免循环

    own = conn is None
    conn = conn or connect()
    init_db(conn)
    created = linked = 0
    for code, notes in POLICY_NOTES.items():
        for n in notes:
            _, is_new, links = add_event(
                n.get("date"), n.get("title"), n.get("impact", ""), n.get("direction"),
                n.get("source", ""), n.get("source_url", ""),
                stocks=[code], conn=conn)
            created += int(is_new)
            linked += links
    for industry, notes in INDUSTRY_POLICY.items():
        for n in notes:
            _, is_new, links = add_event(
                n.get("date"), n.get("title"), n.get("impact", ""), n.get("direction"),
                n.get("source", ""), n.get("source_url", ""),
                industries=[industry], conn=conn)
            created += int(is_new)
            linked += links
    if verbose:
        print(f"seed 完成：新建事件 {created} 条，新增关联 {linked} 条")
    if own:
        conn.close()
    return created, linked


# ---------------------------------------------------------------- 读

def list_events(limit=50, code=None, industry=None, status="active", since=None,
                month=None, conn=None):
    own = conn is None
    conn = conn or connect()
    sql = ("SELECT e.*, GROUP_CONCAT(l.target_type || ':' || l.target_key) AS targets "
           "FROM events e LEFT JOIN event_links l ON l.event_id = e.id ")
    where, args = [], []
    if code or industry:
        sub, sub_args = [], []
        if code:
            sub.append("(l.target_type='stock' AND l.target_key=?)")
            sub_args.append(str(code))
        if industry:
            sub.append("(l.target_type='industry' AND l.target_key=?)")
            sub_args.append(str(industry))
        where.append("e.id IN (SELECT event_id FROM event_links WHERE " + " OR ".join(sub) + ")")
        args.extend(sub_args)
    if status:
        where.append("e.status = ?")
        args.append(status)
    if since:
        where.append("e.event_date >= ?")
        args.append(str(since))
    if month:
        where.append("substr(e.event_date,1,7) = ?")
        args.append(str(month))
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "GROUP BY e.id ORDER BY e.event_date DESC, e.id DESC LIMIT ?"
    args.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    if own:
        conn.close()
    return rows


def notes_for(code, industry_name="", limit=20, conn=None):
    """给估值页的接口：个股事件 + 行业事件（行业按关键词包含匹配，与老逻辑一致），去重后倒序。

    返回字段与 policy_notes 保持一致（date/title/impact/direction/source），前端零改动。
    """
    try:
        own = conn is None
        conn = conn or connect()
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
        if not has_table:
            if own:
                conn.close()
            return None

        rows = []
        if code:
            rows += conn.execute(
                "SELECT DISTINCT e.* FROM events e JOIN event_links l ON l.event_id=e.id "
                "WHERE l.target_type='stock' AND l.target_key=? AND e.status='active'",
                (str(code),)).fetchall()
        for r in conn.execute(
                "SELECT DISTINCT e.* FROM events e JOIN event_links l ON l.event_id=e.id "
                "WHERE l.target_type='industry' AND e.status='active'").fetchall():
            # 行业名是关键词（如"小金属"），行业全名可能是"小金属Ⅱ"
            key = conn.execute("SELECT target_key FROM event_links WHERE event_id=? AND target_type='industry'",
                               (r["id"],)).fetchone()
            if key and industry_name and key["target_key"] in industry_name:
                rows.append(r)
        if own:
            conn.close()
    except sqlite3.Error:
        return None

    seen, out = set(), []
    for r in sorted(rows, key=lambda x: (x["event_date"], x["id"]), reverse=True):
        key = (r["event_date"], r["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": r["id"],
            "date": r["event_date"],
            "title": r["title"],
            "impact": r["summary"],
            "direction": r["direction"],
            "source": r["source"],
            "source_url": r["source_url"],
        })
        if len(out) >= limit:
            break
    return out


def timeline(days=365, industry=None, month=None, max_months=24, conn=None):
    """事件时间线：按月 × 行业聚合（给 /events 页面用）

    计数口径：COUNT(DISTINCT event_id)——一条事件挂 4 只钨股 + 1 个行业，行业维度只记 1 次，
    不会因为关联多而把柱子撑大。没有挂行业的事件单独归到「未分类」。
    """
    from datetime import date, timedelta

    own = conn is None
    conn = conn or connect()
    init_db(conn)
    today = date.today()
    since = (today - timedelta(days=int(days))).isoformat()

    per_ind = {}
    for r in conn.execute(
            "SELECT substr(e.event_date,1,7) ym, l.target_key ind, COUNT(DISTINCT e.id) n "
            "FROM events e JOIN event_links l ON l.event_id=e.id "
            "WHERE e.status='active' AND e.event_date>=? AND l.target_type='industry' "
            + ("AND l.target_key=? " if industry else "") +
            "GROUP BY ym, ind", ([since, industry] if industry else [since])).fetchall():
        per_ind.setdefault(r["ind"], {})[r["ym"]] = r["n"]

    if not industry:
        unclassified = {}
        for r in conn.execute(
                "SELECT substr(event_date,1,7) ym, COUNT(*) n FROM events e "
                "WHERE e.status='active' AND event_date>=? AND id NOT IN "
                "(SELECT event_id FROM event_links WHERE target_type='industry') "
                "GROUP BY ym", (since,)).fetchall():
            unclassified[r["ym"]] = r["n"]
        if unclassified:
            per_ind["未分类"] = unclassified

    dirs = {r["direction"]: r["n"] for r in conn.execute(
        ("SELECT e.direction, COUNT(*) n FROM events e WHERE e.status='active' AND e.event_date>=? "
         if not industry else
         "SELECT e.direction, COUNT(DISTINCT e.id) n FROM events e "
         "JOIN event_links l ON l.event_id=e.id WHERE e.status='active' AND e.event_date>=? "
         "AND l.target_type='industry' AND l.target_key=? ")
        + "GROUP BY e.direction",
        ((since,) if not industry else (since, industry))).fetchall()}

    # 月份轴：从 since 月起连续到今天；超过 max_months 时保留最近的月份（否则当月会被挤掉）
    sy, sm = map(int, since[:7].split("-"))
    ty, tm = today.year, today.month
    months = []
    y, m = sy, sm
    while (y, m) <= (ty, tm):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    months = months[-max_months:]

    series = []
    for ind in sorted(per_ind, key=lambda k: -sum(per_ind[k].values())):
        series.append({"name": ind, "data": [per_ind[ind].get(ym, 0) for ym in months]})

    events = list_events(limit=200 if month else 80, industry=industry,
                         since=since, month=month, conn=conn)
    if own:
        conn.close()
    return {
        "months": months,
        "series": series,
        "directions": {d: dirs.get(d, 0) for d in DIRECTIONS},
        "total": sum(dirs.values()),
        "industries": [{"name": s["name"], "total": sum(s["data"])} for s in series],
        "events": events,
        "since": since,
        "month": month,
        "month_total": len(events),
    }


def stats(conn=None):
    own = conn is None
    conn = conn or connect()
    init_db(conn)
    s = {
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "links": conn.execute("SELECT COUNT(*) FROM event_links").fetchone()[0],
        # 孤儿关联：event 已被删但 link 还在（外部用裸 sqlite3 连接删数据时会漏掉 cascade）
        "orphan_links": conn.execute(
            "SELECT COUNT(*) FROM event_links WHERE event_id NOT IN (SELECT id FROM events)").fetchone()[0],
        "stocks": conn.execute("SELECT COUNT(DISTINCT target_key) FROM event_links WHERE target_type='stock'").fetchone()[0],
        "industries": conn.execute("SELECT COUNT(DISTINCT target_key) FROM event_links WHERE target_type='industry'").fetchone()[0],
        "db": db_path(),
    }
    if own:
        conn.close()
    return s


# ---------------------------------------------------------------- AI 抽取

EXTRACT_PROMPT = """你是财经新闻结构化助手。从用户提供的文字中抽取一条「产业政策 / 公司事件」，只输出一个 JSON 对象，不要解释、不要 markdown。

字段要求：
- date: 事件发生日期，格式 YYYY-MM-DD；原文没有明确日期就填今天（{today}）
- title: 事件标题，不超过 40 字，动词开头
- summary: 对基本面/估值的影响，120 字以内；只能写原文有依据的内容，原文没有的数字一律不要编
- direction: 只能是 "利好" / "利空" / "中性" 三者之一
- source: 媒体名 + 日期（原文有就写，没有就空字符串）
"""


def extract_event(text, api_key=None, poster=None, today=None):
    """把一段新闻文字抽成结构化事件（不写库）。

    poster: 可注入的 HTTP 函数，签名 poster(url, headers, payload, timeout) -> dict，
            测试时用假函数即可离线跑。默认用 requests。
    返回 (event_dict, usage_dict)；失败抛 RuntimeError。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("待抽取的文字为空")
    today = today or datetime.now().strftime("%Y-%m-%d")
    prompt = EXTRACT_PROMPT.replace("{today}", today)

    if api_key is None:
        try:
            from config import DEEPSEEK_API_KEY as api_key   # config 会覆盖 config_local
        except Exception:
            api_key = ""
    if not api_key:
        raise RuntimeError("没有可用的 DeepSeek key（config_local.DEEPSEEK_API_KEY 为空）")

    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": text[:6000]}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    if poster is None:
        import requests

        def _default_poster(url, headers, payload, timeout):
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json()
        poster = _default_poster

    data = poster("https://api.deepseek.com/chat/completions", headers, payload, 60)
    try:
        content = data["choices"][0]["message"]["content"]
        event = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise RuntimeError(f"模型返回不是合法 JSON：{e}")

    cleaned = {
        "date": str(event.get("date") or today)[:10],
        "title": str(event.get("title") or "").strip()[:80],
        "summary": str(event.get("summary") or "").strip()[:400],
        "direction": _direction(event.get("direction")),
        "source": str(event.get("source") or "").strip()[:120],
    }
    if not cleaned["title"]:
        raise RuntimeError("模型没有给出 title，拒绝写入")
    return cleaned, data.get("usage") or {}
