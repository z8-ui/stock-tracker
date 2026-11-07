# -*- coding: utf-8 -*-
"""财经快讯抓取 + 关键词筛选（只产候选，不写 events 表）

为什么单独一个文件
------------------
抓取端会变（接口改版、被墙、加签名），入库端要稳（必须人确认）。所以拆开：
  这里只负责"把网上标题变成候选列表 + 记下看过哪些"
  写库仍然走 events_cli add / add-text

已实测可用的源（2026-11）：
  东财快讯  https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html
            返回 JSONP 前缀 var ajaxResult={...}；50 条/次；digest 就是快讯全文
  新浪财经  https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516
            标准 JSON；20 条/次；只有 intro 摘要，正文要另抓
  财联社    /nodeapi/telegraphList 直连 404（要 sign），不做，避免硬编码 token

去重
----
feed_seen 表记住"这条标题已经给你看过了"，默认只显示新的；decision 记 accepted/skipped/shown，
这样第二天再跑不会重复问你同一批快讯。
"""

import hashlib
import json
import re
import urllib.request
from datetime import datetime, timedelta

import events_store

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

EASTMONEY_URL = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"
SINA_URL = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num={num}&page=1"

# 强相关：政策/产业面（能改变供给或规则的）
POLICY_KEYWORDS = [
    "出口管制", "出口许可", "关税", "加征", "配额", "补贴", "反倾销", "反补贴", "禁运",
    "收储", "限产", "环保督察", "安监", "专项债", "降准", "降息", "LPR", "专项规划",
    "制裁", "实体清单", "国产替代", "新质生产力", "十五五", "规划", "指导意见", "管理办法",
]
# 弱相关：公司/市场层面（有信息量，但通常不是"政策"）
MARKET_KEYWORDS = [
    "涨价", "提价", "复产", "重组", "并购", "立案", "退市", "中标", "订单", "扩产",
    "投产", "业绩预告", "回购", "增持", "减持", "招标",
]
DEFAULT_KEYWORDS = POLICY_KEYWORDS + MARKET_KEYWORDS

SEEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_seen (
    fp            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL DEFAULT '',
    first_seen    TEXT NOT NULL,
    decision      TEXT NOT NULL DEFAULT 'pending',
    review_status TEXT NOT NULL DEFAULT 'pending',
    process_status TEXT NOT NULL DEFAULT 'pending',
    event_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_feed_seen_time ON feed_seen(first_seen DESC);
"""


# ---------------------------------------------------------------- 抓取

def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://finance.eastmoney.com/"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _clean(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _strip_repeat_prefix(title, body):
    """东财快讯的 digest 常以【标题】开头，去掉以免同一句话出现两次。"""
    t = (title or "").strip()
    if body and t and body.lstrip().startswith("【" + t + "】"):
        body = body.lstrip()[len(t) + 2:].strip()
    return body


def fetch_eastmoney(limit=50):
    raw = _get(EASTMONEY_URL).decode("utf-8", "replace")
    payload = raw.split("=", 1)[1].strip().rstrip(";")
    data = json.loads(payload)
    out = []
    for it in (data.get("LivesList") or [])[:limit]:
        title = _clean(it.get("title"))
        if not title:
            continue
        out.append({
            "source": "东财快讯",
            "source_key": "eastmoney",
            "title": title,
            "summary": _strip_repeat_prefix(title, _clean(it.get("digest"))),
            "url": it.get("url_w") or "",
            "published_at": (it.get("showtime") or "")[:16],
        })
    return out


def fetch_sina(limit=20):
    raw = _get(SINA_URL.format(num=max(1, min(limit, 50)))).decode("utf-8", "replace")
    data = json.loads(raw)
    out = []
    for it in (data.get("result", {}).get("data") or [])[:limit]:
        title = _clean(it.get("title"))
        if not title:
            continue
        try:
            ts = datetime.fromtimestamp(int(it.get("ctime") or 0))
            when = ts.strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            when = ""
        out.append({
            "source": "新浪财经",
            "source_key": "sina",
            "title": title,
            "summary": _clean(it.get("intro") or it.get("summary") or ""),
            "url": it.get("url") or "",
            "published_at": when,
        })
    return out


SOURCES = {"eastmoney": fetch_eastmoney, "sina": fetch_sina}


def fetch_headlines(sources=None, limit=50):
    """按源抓取，返回 (items, errors, ok_sources)。

    ok_sources 是**成功来源数**（返回空列表也算成功）：CLI 靠它区分
    “全部源都挂了”和“源正常、今天确实没新闻”——只看 items 为空会把故障当安静。
    """
    keys = sources or list(SOURCES)
    items, errors, ok = [], [], 0
    for k in keys:
        fn = SOURCES.get(k)
        if not fn:
            errors.append(f"未知源: {k}")
            continue
        try:
            got = fn(limit=limit)
            if not isinstance(got, list):
                raise ValueError("源返回结构异常")
            items.extend(got)
            ok += 1                                 # 空列表也算这个源成功
        except Exception as e:                      # 网络问题不该让整个 feed 挂掉
            errors.append(f"{k}: {type(e).__name__} {str(e)[:60]}")
    return items, errors, ok


# ---------------------------------------------------------------- 筛选 / 去重

def _fp(item):
    raw = f"{item.get('source_key','')}|{item.get('title','').strip()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def industry_keywords(conn=None):
    """把库里已有的行业关键词也加入筛词（如 小金属/稀土）。"""
    try:
        own = conn is None
        conn = conn or events_store.connect()
        rows = conn.execute(
            "SELECT DISTINCT target_key FROM event_links WHERE target_type='industry'").fetchall()
        if own:
            conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def _watch_codes():
    """自选池里的 6 位代码（命中说明这条直接关系到盯着的票）。"""
    try:
        import config
        codes = set()
        for attr in ("STOCK_WATCHLIST", "VALUATION_WATCHLIST"):
            v = getattr(config, attr, None)
            if isinstance(v, dict):
                codes |= {str(x) for x in v.values()}
            elif isinstance(v, (list, tuple)):
                for it in v:
                    codes |= {str(x) for x in (it.values() if isinstance(it, dict) else [it])}
        return codes
    except Exception:
        return set()


def screen(items, keywords=None):
    """关键词命中 → 打上 weight（2=政策/行业/自选股，1=公司/市场面），无关的丢掉。

    宁可漏，也别把娱乐新闻和"某明星演唱会"塞给模型。
    """
    kws_policy = POLICY_KEYWORDS + list(keywords or [])
    kws_market = MARKET_KEYWORDS
    ind = industry_keywords()
    watch = _watch_codes()
    out = []
    for it in items:
        blob = f"{it['title']} {it.get('summary', '')}"
        strong = [k for k in kws_policy if k and k in blob]
        strong += [k for k in ind if k and k in blob]
        weak = [k for k in kws_market if k and k in blob]
        codes = sorted({c for c in CODE_RE.findall(blob) if c in watch})
        if not (strong or weak or codes):
            continue
        it = dict(it)
        it["fp"] = _fp(it)
        it["hits"] = list(dict.fromkeys(strong + weak))[:5]
        it["codes"] = codes
        it["weight"] = 2 if (strong or codes) else 1
        out.append(it)
    return out


def dedupe(items, threshold=0.85):
    """同一件事常被多源/多条重复发（"000520，被立案" / "000520，被证监会立案"）。

    归一化后按 SequenceMatcher 相似度合并，保留信息更全（更长）的那条，并记 dup_count。
    """
    from difflib import SequenceMatcher

    kept, merged = [], 0
    for it in sorted(items, key=lambda x: (-x.get("weight", 1), -len(x.get("title", "")))):
        norm = re.sub(r"[\s，,。！!？?：:、（）()\[\]【】\"'“”‘’·\-—]", "", it.get("title", ""))
        hit = None
        for k in kept:
            if len(norm) < 8 or len(k["_norm"]) < 8:
                continue
            if SequenceMatcher(None, norm, k["_norm"]).ratio() >= threshold:
                hit = k
                break
        if hit:
            hit["dup_count"] = hit.get("dup_count", 1) + 1
            merged += 1
        else:
            it = dict(it)
            it["_norm"] = norm
            it["dup_count"] = 1
            kept.append(it)
    for k in kept:
        k.pop("_norm", None)
    return kept, merged


def init_seen(conn=None):
    own = conn is None
    conn = conn or events_store.connect()
    conn.executescript(SEEN_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(feed_seen)").fetchall()}
    migrated = False
    for name, decl in (("event_id", "INTEGER"),
                       ("review_status", "TEXT NOT NULL DEFAULT 'pending'"),
                       ("process_status", "TEXT NOT NULL DEFAULT 'pending'")):
        if name not in cols:
            try:
                conn.execute(f"ALTER TABLE feed_seen ADD COLUMN {name} {decl}")
                migrated = migrated or name in ("review_status", "process_status")
            except Exception:
                pass

    # 旧 decision → 两维状态迁移：只在补列那一次执行。
    # 若每次 init 都跑，会把后来人工写入的 rejected/approved（decision 列仍为 pending）
    # 又刷回 pending —— 人工结论被自动“洗白”比丢候选更严重。
    if migrated:
        conn.execute("UPDATE feed_seen SET process_status='extract_failed', "
                     "review_status='pending' WHERE decision='extract_failed'")
        conn.execute("UPDATE feed_seen SET process_status='ingested', review_status='approved' "
                     "WHERE decision IN ('ingested','accepted')")
        conn.execute("UPDATE feed_seen SET process_status='pending', review_status='pending' "
                     "WHERE decision IN ('shown','skipped','pending')")
    conn.commit()
    if own:
        conn.close()


def seen_fps(conn=None):
    """已经**人工处理完**的候选（approved / rejected / ingested）。

    这些才该从待处理列表里消失；pending 和 extract_failed 必须继续出现。
    """
    init_seen(conn)
    own = conn is None
    conn = conn or events_store.connect()
    s = {r[0] for r in conn.execute(
        "SELECT fp FROM feed_seen WHERE review_status IN ('approved','rejected') "
        "OR process_status='ingested'")}
    if own:
        conn.close()
    return s


def mark(items, process="pending", conn=None, event_id=None, review=None):
    """更新候选状态（双维度）：

    review_status : pending（待人工审核）/ approved / rejected —— 只有人能改
    process_status: pending / extract_failed / ingested —— 机器处理进度

    要点：
      - 展示候选只写 process=pending，绝不改 review_status
      - 已 approved/rejected 的候选，后续采集不能覆盖人工结论（rule 8）
      - extract_failed 保留 review=pending，下次继续出现
    """
    init_seen(conn)
    own = conn is None
    conn = conn or events_store.connect()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for it in items:
        fp = it.get("fp") or _fp(it)
        conn.execute(
            "INSERT INTO feed_seen (fp,source,title,url,first_seen,decision,"
            "review_status,process_status,event_id) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(fp) DO UPDATE SET "
            "process_status=CASE "
            "  WHEN feed_seen.process_status='ingested' "
            "       AND excluded.process_status IN ('pending','extract_failed') "
            "  THEN feed_seen.process_status ELSE excluded.process_status END, "
            "review_status=CASE "
            "  WHEN feed_seen.review_status IN ('approved','rejected') "
            "       AND excluded.review_status='pending' THEN feed_seen.review_status "
            "  ELSE COALESCE(excluded.review_status, feed_seen.review_status) END, "
            "event_id=COALESCE(excluded.event_id, feed_seen.event_id)",
            (fp, it.get("source", ""), it.get("title", ""), it.get("url", ""),
             now, process, review or "pending", process,
             event_id if event_id is not None else it.get("event_id")))
        n += 1
    conn.commit()
    if own:
        conn.close()
    return n


def pending_fp(conn=None):
    """未完成人工审核的候选（含抽取失败）。"""
    init_seen(conn)
    own = conn is None
    conn = conn or events_store.connect()
    rows = {r[0] for r in conn.execute(
        "SELECT fp FROM feed_seen WHERE review_status='pending' "
        "AND process_status<>'ingested'")}
    if own:
        conn.close()
    return rows


def decision_stats(conn=None):
    """按两个维度统计：待审核/通过/驳回 × 待处理/抽取失败/已入库。"""
    init_seen(conn)
    own = conn is None
    conn = conn or events_store.connect()
    out = {"review": {}, "process": {}}
    for r in conn.execute("SELECT review_status, COUNT(*) FROM feed_seen GROUP BY review_status"):
        out["review"][r[0]] = r[1]
    for r in conn.execute("SELECT process_status, COUNT(*) FROM feed_seen GROUP BY process_status"):
        out["process"][r[0]] = r[1]
    if own:
        conn.close()
    return out


def recent_seen(days=7, limit=50, conn=None):
    init_seen(conn)
    own = conn is None
    conn = conn or events_store.connect()
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM feed_seen WHERE first_seen>=? ORDER BY first_seen DESC LIMIT ?",
        (since, limit)).fetchall()]
    if own:
        conn.close()
    return rows


def to_event_text(item):
    """给 AI 抽取用的输入：标题 + 摘要（快讯 digest 基本就是全文）。"""
    return f"{item.get('title','')}\n{item.get('summary','')}\n来源: {item.get('source','')} {item.get('published_at','')}"
