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
import re
import sqlite3
import uuid
from contextlib import contextmanager
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
    occurred_at TEXT,
    published_at TEXT,
    collected_at TEXT,
    impact_level  TEXT NOT NULL DEFAULT '待确认',
    mechanism     TEXT NOT NULL DEFAULT '',
    evidence      TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_key  TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT '',
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
    """建表 + 补列 + 补索引（幂等）。

    事务契约（2026-11）：`executescript()` 在执行前会**隐式 COMMIT**，所以
    不允许在调用方已有事务的连接上执行。传入 conn 且 `conn.in_transaction` 为真时
    直接拒绝并给出明确错误 —— 绝不静默提交调用方事务。
    自己新建的连接永远是干净的（own=True），所以正常初始化/迁移流程不受影响。
    """
    own = conn is None
    conn = conn or connect()
    if not own and getattr(conn, "in_transaction", False):
        raise RuntimeError(
            "init_db(conn) 拒绝在已有事务中执行：executescript() 会隐式提交调用方事务。"
            "请在事务开始之前完成初始化，或用独立连接调用 init_db()。")
    try:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        conn.commit()
    finally:
        if own:
            conn.close()


# 写入所需字段（与 SCHEMA 一一对应，含 2026-11 新增的 5 个）
_EVENTS_REQUIRED_COLS = frozenset({
    "id", "event_date", "title", "summary", "direction", "source", "source_url",
    "fingerprint", "status", "occurred_at", "published_at", "collected_at",
    "impact_level", "mechanism", "evidence", "review_status", "created_at"})
_LINKS_REQUIRED_COLS = frozenset({
    "id", "event_id", "target_type", "target_key", "relation_type"})

# 普通索引的真实结构要求（名字 → (所属表, ((列名, 是否DESC), ...))）
# 只看名字是不够的：名字对、表错或列错的索引照样会让查询走错，也会让唯一性/排序失效。
_REQUIRED_INDEXES = {
    "idx_events_date": ("events", (("event_date", True),)),          # event_date DESC
    "idx_links_target": ("event_links", (("target_type", False),     # target_type,
                                         ("target_key", False))),    # target_key（顺序固定）
}


def _safe_ident(name):
    """只允许 SCHEMA 里出现过的标识符进 PRAGMA（PRAGMA 不能参数化）。"""
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or "") else None


def _index_signature(conn, name):
    """索引的真实结构：(所属表, ((列名, 是否DESC), ...), 是否 partial)。

    - 列与排序方向用 `PRAGMA index_xinfo` 读（index_info 不含 DESC）；
      xinfo 行格式 (seqno, cid, name, desc, coll, key)，只取 key=1 的列。
    - 用 `PRAGMA index_list` 顺带取 partial 标记（部分索引不能当完整索引用）。
    索引不存在 / 是表达式索引 / 读不出来 → None。
    """
    name = _safe_ident(name)
    if not name:
        return None
    row = conn.execute("SELECT tbl_name FROM sqlite_master "
                       "WHERE type='index' AND name=?", (name,)).fetchone()
    if not row:
        return None
    table = row[0]
    if not _safe_ident(table):
        return None
    partial = False
    try:
        for meta in conn.execute(f"PRAGMA index_list({table})").fetchall():
            if meta[1] == name:
                partial = bool(meta[4]) if len(meta) > 4 else False
                break
        rows = conn.execute(f"PRAGMA index_xinfo({name})").fetchall()
    except sqlite3.DatabaseError:
        return None
    cols = []
    for r in rows:
        if len(r) >= 6 and not r[5]:        # key=0 是 rowid 辅助行，跳过
            continue
        if r[2] is None:                    # 表达式索引：列名取不到，视为不符合
            return None
        cols.append((r[2], bool(r[3])))
    return table, tuple(cols), partial


def _unique_column_sets(conn, table):
    """该表**完整**（非 partial）UNIQUE 索引的列组合。

    PRAGMA index_list 行格式：(seq, name, unique, origin, partial)。
    只取 unique=1 且 partial=0 的 —— 部分唯一索引（... WHERE ...）只约束一部分行，
    不能当成"完整唯一约束"来用，否则重复数据会漏进来。
    """
    out = set()
    table = _safe_ident(table)
    if not table:
        return out
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    except sqlite3.DatabaseError:
        return out
    for row in rows:
        if not row[2]:                      # unique 标记
            continue
        if len(row) > 4 and row[4]:         # partial 索引 → 不算完整约束
            continue
        idx = _safe_ident(row[1])
        if not idx:
            continue
        try:
            cols = tuple(r[2] for r in conn.execute(f"PRAGMA index_info({idx})").fetchall())
        except sqlite3.DatabaseError:
            continue
        if cols and all(c for c in cols):
            out.add(cols)
    return out


def _schema_ready(conn):
    """当前库是否已经能直接写入（表 / 列 / 唯一约束 / 索引结构 全对得上 SCHEMA）。

    批量写入时不能随便调 init_db：它走 `executescript`，会在执行前隐式 COMMIT，
    把外层事务拦腰截断。所以必须把"结构是否真的就绪"判准 ——
    只查 events 的几张列是不够的（events 齐了但 event_links 不存在时曾误判为就绪，
    写关联就会炸）；只查索引名字也不够（名字对、建在错的表/列上，排序与过滤都会失效）。

    检查项：
      1. events / event_links 两张表都存在；
      2. events 写入所需字段齐全（含 published_at / collected_at）；
      3. event_links 所需字段齐全（含 relation_type）；
      4. events.fingerprint 与 event_links(event_id,target_type,target_key) 的**完整**唯一约束在
         （partial unique index 不算）；
      5. idx_events_date = events(event_date DESC)、
         idx_links_target = event_links(target_type, target_key) 按真实结构与顺序匹配，
         且都不是 partial 索引。
    """
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"events", "event_links"} <= tables:
            return False
        ecols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        if not _EVENTS_REQUIRED_COLS <= ecols:
            return False
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(event_links)").fetchall()}
        if not _LINKS_REQUIRED_COLS <= lcols:
            return False
        if ("fingerprint",) not in _unique_column_sets(conn, "events"):
            return False
        if ("event_id", "target_type", "target_key") not in _unique_column_sets(conn, "event_links"):
            return False
        # 索引：不只看名字，比对 (所属表, 列, 顺序, DESC) 与 partial 标记
        for name, expected in _REQUIRED_INDEXES.items():
            sig = _index_signature(conn, name)
            if sig is None:
                return False
            table, cols, partial = sig
            if partial or (table, cols) != expected:
                return False
        return True
    except sqlite3.DatabaseError:
        return False


@contextmanager
def transaction(conn):
    """批量写入的事务边界，**纯 SAVEPOINT 语义，不碰调用方事务**。

    - 成功：只 RELEASE 本层。若本层就是最外层 SAVEPOINT，SQLite 规定 RELEASE
      即提交整个事务；若外面已经有事务，则只是释放嵌套层 —— 调用方的东西不会被提交。
    - 失败：ROLLBACK TO 撤销本层写入，再 RELEASE 释放本层，异常原样抛出；
      不 commit、不 rollback，调用方的事务状态由调用方自己决定。
    - 无论成功失败，异常路径都不会留下未释放的 SAVEPOINT。
    """
    name = "sp_" + uuid.uuid4().hex[:12]
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")


def _ensure_schema(conn, own):
    """确保表结构就绪，并且绝不在调用方事务里做会隐式提交的 DDL。

    init_db() 走 `executescript()`，sqlite3 在执行前会隐式 COMMIT —— 这会提交
    调用方正在写的事务。所以分三种情况：
      1. 结构已就绪（表/列/唯一约束/索引 都对得上）→ 什么都不做（不重复迁移）；
      2. 结构不完整 + 连接在事务里 → 明确拒绝报错（不静默提交）；
      3. 结构不完整 + 无外层事务 → 就地 init_db（自己开的连接一定是这种），
         迁移完再复核一次；仍不合格说明无法自动修复（例如缺唯一约束），直接报错，
         绝不让后续写入在半成品结构上"看起来成功"。
    """
    if _schema_ready(conn):
        return
    if not own and getattr(conn, "in_transaction", False):
        raise RuntimeError(
            "事件库结构不完整（缺表/缺列/缺唯一约束/索引结构不符），而当前连接已在事务中："
            "init_db() 的 executescript() 会隐式提交调用方事务，因此拒绝在事务内迁移。"
            "请先在事务外调用 events_store.init_db(conn) 完成迁移，再执行 seed。")
    init_db(conn)
    if not _schema_ready(conn):
        raise RuntimeError(
            "事件库结构不完整且无法自动迁移（缺表/缺列/缺唯一约束/索引结构不符）："
            f"{db_path()}。init_db() 只能建缺失的表与索引、补缺失的列，"
            "改不了已存在的约束与同名但结构错误的索引；请人工检查或从备份恢复。")


def _ensure_columns(conn):
    """老库补列（SQLite 加列可空，不动历史数据）。

    occurred_at  : 事件实际发生日期（YYYY-MM-DD）或 NULL —— 不知道就是 NULL
    published_at : 来源发布时间（YYYY-MM-DD HH:MM）或 NULL
    collected_at : 系统采集时间（YYYY-MM-DD HH:MM:SS），只在入库时写一次
    impact_level / mechanism / evidence / review_status :
                   个股政策分析所需的五个字段（2026-11 新增）。
                   老数据的默认值是"待确认"/空，不是"利好"——宁可显示待确认，
                   也不能凭空给一条没人核实过的结论。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    for name, decl in (("occurred_at", "TEXT"), ("published_at", "TEXT"),
                       ("collected_at", "TEXT"),
                       ("impact_level", "TEXT NOT NULL DEFAULT '待确认'"),
                       ("mechanism", "TEXT NOT NULL DEFAULT ''"),
                       ("evidence", "TEXT NOT NULL DEFAULT ''"),
                       ("review_status", "TEXT NOT NULL DEFAULT 'pending'")):
        if name not in cols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {name} {decl}")
    # 回填：老数据只有一个 event_date，只能视为“发生日期”（当时的口径）；
    # collected_at 用 created_at；published_at 不知道，保持 NULL，不拿别的字段冒充。
    conn.execute("UPDATE events SET occurred_at = event_date "
                 "WHERE occurred_at IS NULL AND event_date <> ''")
    conn.execute("UPDATE events SET collected_at = created_at WHERE collected_at IS NULL")

    # event_links.relation_type：股票直连=direct，行业=industry（老数据按目标类型回填）
    lcols = {r[1] for r in conn.execute("PRAGMA table_info(event_links)").fetchall()}
    if "relation_type" not in lcols:
        conn.execute("ALTER TABLE event_links ADD COLUMN relation_type TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE event_links SET relation_type='direct' "
                     "WHERE relation_type='' AND target_type='stock'")
        conn.execute("UPDATE event_links SET relation_type='industry' "
                     "WHERE relation_type='' AND target_type='industry'")


def _norm_date(value):
    """只接受 YYYY-MM-DD；空/非法 → None（绝不返回今天）。"""
    s = str(value or "").strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def _norm_datetime(value):
    """接受 YYYY-MM-DD 或 YYYY-MM-DD HH:MM[[:SS]]；非法 → None。"""
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(s)], fmt).strftime(
                "%Y-%m-%d %H:%M" if fmt != "%Y-%m-%d" else "%Y-%m-%d")
        except ValueError:
            continue
    return None


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

def add_event(event_date=None, title=None, summary="", direction="中性", source="",
              source_url="", stocks=(), industries=(), status="active",
              occurred_at=None, published_at=None, collected_at=None, conn=None,
              impact_level="待确认", mechanism="", evidence="", review_status="pending",
              commit=True):
    """新增一条事件并挂标的；同日同标题已存在则只补挂缺失标的。

    三个时间字段语义严格区分（2026-11 整改）：
      occurred_at  : 事件发生时间，原文没写就是 None —— 绝不拿今天/采集时间补齐
      published_at : 来源发布时间，可能只有日期，也可能是 None
      collected_at : 入库时间，由本函数写，重启/重跑不变

    impact_level / mechanism / evidence / review_status（2026-11 新增）：
      没有可靠依据就保持 "待确认" / 空串 / "pending"，由人工或分析模块补，
      这里不做任何推测性填充 —— 同行业 ≠ 直接受益。
    审核状态只有人能改：CLI 里 --yes 入库的路径显式传 "approved"。

    event_date 列保留为 NOT NULL 兼容老表：occurred_at 未知时写 ''，
    绝不能为了满足约束塞今天。

    commit=False（2026-11 seed 原子性修复）：
      只做语句、不提交、不建表，事务交给外层（见 transaction()）。
      批量调用必须这样用 —— 否则每写一条就 commit，中途失败会留下半截数据。

    返回 (event_id, created: bool, new_links: int)
    """
    occurred = _norm_date(occurred_at if occurred_at is not None else event_date)
    published = _norm_datetime(published_at)
    collected = _norm_datetime(collected_at) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    legacy_date = occurred or ""

    title = (title or "").strip()
    if not title:
        raise ValueError("title 不能为空")

    own = conn is None
    if own and not commit:
        raise ValueError(
            "add_event(conn=None, commit=False) 是危险组合：连接由本函数创建又要求不提交，"
            "关闭时会丢弃这批写入。批量写入请显式传入 conn，并配合 transaction(conn) 使用。")
    conn = conn or connect()
    _ensure_schema(conn, own)
    # 指纹：有发生日期用发生日期，否则用发布时间；都没有就只按标题去重
    fp = _fingerprint(occurred or (published or "")[:10], title)
    cur = conn.execute("SELECT id, occurred_at, published_at, collected_at, "
                       "event_date FROM events WHERE fingerprint = ?", (fp,))
    row = cur.fetchone()
    created = row is None
    if created:
        cur = conn.execute(
            "INSERT INTO events (event_date,title,summary,direction,source,source_url,"
            "fingerprint,status,occurred_at,published_at,collected_at,"
            "impact_level,mechanism,evidence,review_status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (legacy_date, title, summary or "", _direction(direction), source or "",
             source_url or "", fp, status, occurred, published, collected,
             impact_level or "待确认", mechanism or "", evidence or "",
             review_status or "pending",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        event_id = cur.lastrowid
    else:
        event_id = row["id"]
        # 重复入库不覆盖已有信息：只补空字段（补出来的值也要有出处，否则不动）
        updates = {}
        if row["occurred_at"] is None and occurred:
            updates["occurred_at"] = occurred
            updates["event_date"] = occurred
        if row["published_at"] is None and published:
            updates["published_at"] = published
        if updates:
            sets = ",".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE events SET {sets} WHERE id=?",
                         list(updates.values()) + [event_id])

    new_links = 0
    for ttype, tkey in _norm_targets(stocks, industries):
        rel = "direct" if ttype == "stock" else "industry"
        try:
            conn.execute("INSERT INTO event_links (event_id,target_type,target_key,relation_type) "
                         "VALUES (?,?,?,?)", (event_id, ttype, tkey, rel))
            new_links += 1
        except sqlite3.IntegrityError:
            pass    # 已挂过
    if commit:
        conn.commit()
    if own:
        conn.close()
    return event_id, created, new_links


def seed_from_static(conn=None, verbose=False):
    """把 policy_notes.py 里的老字典导入 DB（幂等，可重复跑）。

    事务边界（2026-11 修复）：一次 seed 的所有 events / event_links 写入
    要么全进、要么全不进。内部 add_event(commit=False) 只发语句不提交，
    由这里统一 commit；任一条失败 → transaction() 整体 ROLLBACK 并把异常抛出去，
    不留半截数据、也不吞异常假装成功（调用方看到的是非 0 退出/异常）。
    """
    from policy_notes import POLICY_NOTES, INDUSTRY_POLICY   # 延迟导入，避免循环

    own = conn is None
    conn = conn or connect()
    created = linked = 0
    try:
        _ensure_schema(conn, own)   # 结构已就绪就不迁移；事务内绝不做隐式提交的 DDL
        with transaction(conn):
            for code, notes in POLICY_NOTES.items():
                for n in notes:
                    _, is_new, links = add_event(
                        n.get("date"), n.get("title"), n.get("impact", ""), n.get("direction"),
                        n.get("source", ""), n.get("source_url", ""),
                        stocks=[code], conn=conn, commit=False,
                        evidence="静态策池（人工维护）", review_status="approved")
                    created += int(is_new)
                    linked += links
            for industry, notes in INDUSTRY_POLICY.items():
                for n in notes:
                    _, is_new, links = add_event(
                        n.get("date"), n.get("title"), n.get("impact", ""), n.get("direction"),
                        n.get("source", ""), n.get("source_url", ""),
                        industries=[industry], conn=conn, commit=False,
                        evidence="静态策池（行业级，非个股直连）", review_status="approved")
                    created += int(is_new)
                    linked += links
    finally:
        if own:
            conn.close()
    if verbose:
        print(f"seed 完成：新建事件 {created} 条，新增关联 {linked} 条")
    return created, linked


# ---------------------------------------------------------------- 读

def list_events(limit=50, code=None, industry=None, status="active", since=None,
                month=None, conn=None, include_undated=False):
    own = conn is None
    conn = conn or connect()
    eff = _eff_date("e")
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
        # 只看发生/发布时间（不再拿采集时间兜底）；未知日期的事件用 include_undated 单独放进来，
        # 否则"不知道哪天发生"会被算成"今天刚发生"。
        if include_undated:
            where.append("(" + eff + " >= ? OR " + eff + " IS NULL)")
        else:
            where.append(eff + " >= ?")
        args.append(str(since))
    if month:
        where.append("substr(" + eff + ",1,7) = ?")
        args.append(str(month))
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += ("GROUP BY e.id ORDER BY COALESCE(NULLIF(e.occurred_at,''), "
            "NULLIF(e.published_at,''), '') DESC, e.id DESC LIMIT ?")
    args.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    if own:
        conn.close()
    return rows


def db_state(conn=None):
    """事件库状态：'unavailable'（没表/打不开）/ 'empty'（有表没数据）/ 'ok'

    调用方靠这个区分"库挂了"和"库里确实没有事件"——两者的正确反应完全不同。
    """
    own = conn is None
    try:
        conn = conn or connect()
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
        if not has_table:
            return "unavailable"
        n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return "ok" if n else "empty"
    except Exception:
        return "unavailable"
    finally:
        if own and conn is not None:
            conn.close()


RELATION_LABEL = {
    "direct": "直接影响",
    "industry": "行业整体影响",
    "industry_indirect": "产业链间接影响",
    "news_only": "仅为一般新闻",
}


def policy_events_for(code, stock_name="", industry_name="", limit=30, conn=None):
    """按个股返回政策事件（含关系类型 / 影响程度 / 机制 / 证据 / 审核状态）。

    返回语义与 notes_for 一致：
      None → 事件库不可用（没表/打不开）
      []   → 库正常，但这只股票确实没有已关联的事件

    关联优先级（高→低）：
      1. direct            事件显式挂了这只股票的代码（人工/CLI 建立，最可靠）
      2. industry          行业关键词命中该公司行业（行业级影响，**不是**个股直接受益）
    不按"同行业"或"公司名出现在标题里"去凑关联：
    两种都会把 A 公司的利好按到 B 公司头上，正是这次要绝掉的错误。

    stock_name 只用于回显，不参与匹配；主营/产业链目前没有结构化字段，
    因此不做产业链级别判定（宁可标 industry，也不编 indirect）。
    """
    own = conn is None
    try:
        conn = conn or connect()
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
        if not has_table:
            return None

        picked = {}

        if code:
            for r in conn.execute(
                    "SELECT e.*, l.target_key AS rel_key FROM events e "
                    "JOIN event_links l ON l.event_id=e.id "
                    "WHERE l.target_type='stock' AND l.target_key=? AND e.status='active'",
                    (str(code),)).fetchall():
                picked.setdefault(r["id"], ("direct", r))

        for r in conn.execute(
                "SELECT e.*, l.target_key AS rel_key FROM events e "
                "JOIN event_links l ON l.event_id=e.id "
                "WHERE l.target_type='industry' AND e.status='active'").fetchall():
            key = (r["rel_key"] or "")
            if industry_name and key and key in industry_name:
                picked.setdefault(r["id"], ("industry", r))
    except sqlite3.Error:
        return None
    finally:
        if own and conn is not None:
            conn.close()

    eff = lambda r: r["occurred_at"] or r["published_at"] or ""
    order = {"direct": 0, "industry": 1, "industry_indirect": 2, "news_only": 3}

    items = list(picked.values())
    items.sort(key=lambda x: (eff(x[1]), x[1]["id"]), reverse=True)   # 先按时间倒序
    items.sort(key=lambda x: order.get(x[0], 9))                      # 再按关系优先级（稳定排序）

    out = []
    for rel, r in items:
        summary = r["summary"] or ""
        if rel == "direct":
            evidence = (r["evidence"] or
                        f"关联依据：事件显式关联本股票（event_links.stock={r['rel_key']}）")
        else:
            evidence = (r["evidence"] or
                        f"关联依据：行业关键词「{r['rel_key']}」命中公司行业「{industry_name}」"
                        f"（行业级影响，不代表个股直接受益）")
        out.append({
            "event_id": r["id"],
            "id": r["id"],                 # 兼容老调用方（notes_for 原字段名）
            "title": r["title"],
            "summary": summary,
            "occurred_at": r["occurred_at"],
            "published_at": r["published_at"],
            "collected_at": r["collected_at"],
            "date_known": bool(r["occurred_at"]),
            "date": r["occurred_at"],
            "relation_type": rel,
            "relation_label": RELATION_LABEL.get(rel, rel),
            "direction": r["direction"],
            "impact_level": r["impact_level"] or "待确认",
            "mechanism": r["mechanism"] or summary,
            "evidence": evidence,
            "source": r["source"],
            "source_url": r["source_url"],
            "review_status": r["review_status"] or "pending",
            "impact": summary,
            "origin": "db",
        })
        if len(out) >= int(limit):
            break
    return out


def notes_for(code, industry_name="", limit=20, conn=None):
    """给估值页的接口：个股事件 + 行业事件。

    2026-11：改为 policy_events_for 的薄封装，字段保持 date/title/impact/direction/source
    （前端零改动），同时多带关系类型/影响程度/机制/证据/审核状态。

    返回 None 与 [] 语义不同（2026-11 整改）：
      None → 事件库不可用（没有表/打不开）→ 调用方应回退静态字典
      []   → 事件库正常，但这只股票确实没有有效事件（例如全部归档）→ 不得回退
             否则归档掉的事件会从静态字典里"复活"到估值页面
    """
    evs = policy_events_for(code, industry_name=industry_name, limit=limit, conn=conn)
    if evs is None:
        return None
    return evs


def _eff_date(alias="e"):
    """统计/筛选统一口径的 SQL 片段：发生时间 → 发布时间。

    2026-11 修复：原来时间线用的是
    COALESCE(occurred_at, published_at, collected_at)，把"我们知道这件事的时间"
    当成"事件发生的时间"。后果是昨天抓到的、原文没写日期的新闻，会被统计成
    "今天发生"，柱子位置和"事件总数"都对不上。
    采集时间只在页面作为元信息展示，不参与任何日期统计。
    """
    return f"COALESCE(NULLIF({alias}.occurred_at,''), NULLIF({alias}.published_at,''))"


def timeline(days=365, industry=None, month=None, max_months=24, conn=None):
    """事件时间线：按月 × 行业聚合（给 /events 页面用）

    计数口径：COUNT(DISTINCT event_id)——一条事件挂 4 只钨股 + 1 个行业，行业维度只记 1 次，
    不会因为关联多而把柱子撑大。没有挂行业的事件单独归到「未分类」。
    发生日期和发布日期都没有的事件不进任何月份，单独计入 undated（未知日期），
    绝不因为 collected_at 是今天就记到本月。
    """
    from datetime import date, timedelta

    own = conn is None
    conn = conn or connect()
    _ensure_schema(conn, own)     # 结构就绪时不做 DDL（不隐式提交调用方事务）
    today = date.today()
    since = (today - timedelta(days=int(days))).isoformat()
    eff = _eff_date("e")

    per_ind = {}
    for r in conn.execute(
            "SELECT substr(" + eff + ",1,7) ym, l.target_key ind, COUNT(DISTINCT e.id) n "
            "FROM events e JOIN event_links l ON l.event_id=e.id "
            "WHERE e.status='active' AND " + eff + ">=? AND l.target_type='industry' "
            + ("AND l.target_key=? " if industry else "") +
            "GROUP BY ym, ind", ([since, industry] if industry else [since])).fetchall():
        per_ind.setdefault(r["ind"], {})[r["ym"]] = r["n"]

    if not industry:
        unclassified = {}
        for r in conn.execute(
                "SELECT substr(" + eff + ",1,7) ym, COUNT(*) n FROM events e "
                "WHERE e.status='active' AND " + eff + ">=? AND id NOT IN "
                "(SELECT event_id FROM event_links WHERE target_type='industry') "
                "GROUP BY ym", (since,)).fetchall():
            unclassified[r["ym"]] = r["n"]
        if unclassified:
            per_ind["未分类"] = unclassified

    dirs = {r["direction"]: r["n"] for r in conn.execute(
        ("SELECT e.direction, COUNT(*) n FROM events e WHERE e.status='active' AND "
         + eff + ">=? "
         if not industry else
         "SELECT e.direction, COUNT(DISTINCT e.id) n FROM events e "
         "JOIN event_links l ON l.event_id=e.id WHERE e.status='active' AND " + eff + ">=? "
         "AND l.target_type='industry' AND l.target_key=? ")
        + "GROUP BY e.direction",
        ((since,) if not industry else (since, industry))).fetchall()}

    # 未知日期桶：发生/发布时间都没有的事件（不进任何月份柱）
    undated_sql = ("SELECT COUNT(DISTINCT e.id) FROM events e "
                   + ("JOIN event_links l ON l.event_id=e.id " if industry else "")
                   + "WHERE e.status='active' AND " + eff + " IS NULL "
                   + ("AND l.target_type='industry' AND l.target_key=? " if industry else ""))
    undated = conn.execute(undated_sql,
                           ((industry,) if industry else ())).fetchone()[0]

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
                         since=since, month=month, conn=conn,
                         include_undated=not month)
    if own:
        conn.close()
    return {
        "months": months,
        "series": series,
        "directions": {d: dirs.get(d, 0) for d in DIRECTIONS},
        "total": sum(dirs.values()),
        "undated": undated,
        "industries": [{"name": s["name"], "total": sum(s["data"])} for s in series],
        "events": events,
        "since": since,
        "month": month,
        "month_total": len(events),
    }


def stats(conn=None):
    own = conn is None
    conn = conn or connect()
    _ensure_schema(conn, own)     # 同上：只读统计不该顺手提交别人的事务
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
- occurred_at: 事件**实际发生**日期，格式 YYYY-MM-DD。原文没有明确写到事件发生时间就填 null，
  绝对不要用今天、采集时间或发布时间代替；不知道就是不知道。
- published_at: 来源**明确给出**的发布时间，格式 YYYY-MM-DD HH:MM（只有日期就写 YYYY-MM-DD）；
  原文没说就填 null。
- title: 事件标题，不超过 40 字，动词开头
- summary: 对基本面/估值的影响，120 字以内；只能写原文有依据的内容，原文没有的数字一律不要编
- direction: 只能是 "利好" / "利空" / "中性" 三者之一
- source: 媒体名（原文有就写，没有就空字符串）

注意：occurred_at 与 published_at 是两件事。政策"今天发布"不等于"今天实施"，
不要把发布时间抄成发生时间。"""


def extract_event(text, api_key=None, poster=None, today=None, published_at=None,
                  collected_at=None):
    """把一段新闻文字抽成结构化事件（不写库）。

    poster: 可注入的 HTTP 函数，签名 poster(url, headers, payload, timeout) -> dict，
            测试时用假函数即可离线跑。默认用 requests。
    published_at: 采集时拿到的来源发布时间（作为提示，模型没给就用它）
    collected_at: 采集时间（默认现在）
    today: 仅为兼容旧调用方保留，**不参与任何日期填充**（防止“未知日期→今天”回归）

    返回 (event_dict, usage_dict)；失败抛 RuntimeError。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("待抽取的文字为空")
    prompt = EXTRACT_PROMPT

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

    occurred = _norm_date(event.get("occurred_at"))
    if occurred is None and "date" in event:
        occurred = _norm_date(event.get("date"))
    published = _norm_datetime(event.get("published_at")) or _norm_datetime(published_at)
    collected = _norm_datetime(collected_at) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cleaned = {
        "occurred_at": occurred,          # None = 原文没写，绝不补今天
        "published_at": published,        # None = 来源没给
        "collected_at": collected,        # 系统采集时间
        "date": occurred,                 # 兼容老调用方（与 occurred_at 同值，可为 None）
        "title": str(event.get("title") or "").strip()[:80],
        "summary": str(event.get("summary") or "").strip()[:400],
        "direction": _direction(event.get("direction")),
        "source": str(event.get("source") or "").strip()[:120],
    }
    if not cleaned["title"]:
        raise RuntimeError("模型没有给出 title，拒绝写入")
    return cleaned, data.get("usage") or {}