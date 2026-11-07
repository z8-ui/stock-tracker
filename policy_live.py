# -*- coding: utf-8 -*-
"""按个股实时检索政策/事件线索（只找线索，不下结论）

为什么单独一个模块：
  事件库里的条目是人工审核过的结论；这里的条目是"刚抓到的公开材料"。
  两者混在一起会让"没人看过的新闻"长得像"已确认的利好"，所以：
    - 一律 review_status='pending'
    - 一律 impact_level='待确认'，direction 不知道就写 '待确认'
    - 每条都带 origin='live' + 真实 source_url，前端能一眼分开
    - 只读不写库：检索结果不落 events 表，避免污染已审核数据

两路来源（都是公开接口，失败就返回空，绝不编）：
  1. 东财公告  np-anotice-stock.eastmoney.com  → relation_type='direct'（公司级公告）
  2. 市场快讯  复用 event_feed 的东财/新浪源  → relation_type='news_only'（标题提到该公司）
     快讯只能证明"媒体提到了它"，不能证明"政策影响它"，所以不给 direct。

进程内 5 分钟缓存：连点切换股票不该把外部接口打穿。
"""

import re
import time
import urllib.parse

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
ANN_URL = ("https://np-anotice-stock.eastmoney.com/api/security/ann"
           "?sr=-1&page_size={n}&page_index=1&ann_type=A&client_source=web&stock_list={code}")
ANN_DETAIL = "https://data.eastmoney.com/notices/detail/{code}/{art}.html"

TTL = 300
_CACHE = {}          # (kind, key) -> (ts, payload)


def _cached(kind, key, fn):
    now = time.time()
    hit = _CACHE.get((kind, key))
    if hit and now - hit[0] < TTL:
        return hit[1]
    val = fn()
    # 失败也缓存（短一些），避免网络挂了每次点都等 8 秒
    _CACHE[(kind, key)] = (now if val else now, val)
    return val


def clear_cache():
    _CACHE.clear()


def _clean_title(title, name=""):
    t = (title or "").strip()
    if name:
        # "贵州茅台:贵州茅台关于召开…公告" → 去掉重复的公司前缀
        for prefix in (name + ":", name + "：", name):
            if t.startswith(prefix):
                t = t[len(prefix):].strip()
                break
    return t


def _norm_date(value):
    """'2026-11-03 00:00:00' → '2026-11-03'；认不出来就 None（不猜）。"""
    s = str(value or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


# ---- 关联类型（V2.0.1 P1）：口径固定，前后端共用同一套字面量 ----
RELATION_LABEL = {
    "direct": "直接影响（公司公告）",
    "business_related": "主营业务相关",
    "industry_indirect": "产业链/行业间接",
    "macro_related": "宏观政策影响",
    "unconfirmed": "关联待确认",
}
RELATION_PRIORITY = {"direct": 0, "business_related": 1, "industry_indirect": 2,
                     "macro_related": 3, "unconfirmed": 4}


def _classify_title(title, name, industry, keywords):
    """纯字面匹配，按证据强弱给出 (relation_type, 依据说明, 命中词)。

    宁可标 unconfirmed，也不把"媒体提到了这家公司"升级成"直接影响"。
    """
    title = str(title or "")
    if name and len(name) >= 2 and name in title:
        return "unconfirmed", f"标题命中公司名「{name}」（媒体提到不等于政策/公告依据）", name
    for kw in keywords or []:
        if kw and len(kw) >= 2 and kw in title:
            return "business_related", f"标题命中主营业务关键词「{kw}」（关键词来自 F10 经营范围）", kw
    if industry and len(industry) >= 2 and industry in title:
        return "industry_indirect", f"标题命中行业名「{industry}」", industry
    return None, "", ""


def _mechanism(relation, matched):
    """影响机制只描述"可能的传导路径"，不替用户判方向、不给买卖结论。"""
    if relation == "direct":
        return ("公司公告属于公司层面的事实性披露；对经营的实际影响需阅读公告原文判断，"
                "自动检索不代为下结论。")
    if relation == "business_related":
        return (f"该条与主营业务关键词「{matched}」相关，可能经产品价格、原材料成本、"
                f"订单需求或产能传导到经营；是否真的构成影响需核对原文，当前仅有标题依据。")
    if relation == "industry_indirect":
        return (f"该条属于行业/产业链层面（「{matched}」），对个股是间接影响，"
                f"需结合该公司业务占比确认，不能等同于个股利好或利空。")
    return "关联依据不足（仅标题命中，未见政策原文或公司公告），暂不判断影响方向。"


def fetch_announcements(code, name="", limit=10, timeout=8, with_status=False):
    """公司公告（沪深交易所披露，东财汇总）—— 最强的一类"公司级事件"依据。"""
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return []

    def _do():
        url = ANN_URL.format(n=max(1, min(int(limit), 30)), code=code)
        try:
            r = requests.get(url, headers={"User-Agent": UA,
                                           "Referer": "https://data.eastmoney.com/"},
                             timeout=timeout)
            if r.status_code != 200:
                return [], f"failed: HTTP {r.status_code}"
            data = r.json()
        except Exception as e:
            return [], f"failed: {type(e).__name__}"
        rows = ((data or {}).get("data") or {}).get("list") or []
        out = []
        for it in rows:
            art = it.get("art_code") or ""
            pub = _norm_date(it.get("notice_date") or it.get("display_time"))
            title = _clean_title(it.get("title") or it.get("title_ch"), name)
            if not title:
                continue
            col = ""
            cols = it.get("columns") or []
            if cols and isinstance(cols, list):
                col = (cols[0] or {}).get("column_name") or ""
            out.append({
                "event_id": f"live-ann-{art}",
                "title": title,
                "summary": "",
                "occurred_at": None,          # 公告日期不是"事件发生日期"
                "published_at": pub,
                "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "date_known": False,
                "date": None,
                "relation_type": "direct",
                "relation_label": "直接影响（公司公告）",
                "relation_basis": "沪深交易所披露、东财汇总的公司公告",
                "direction": "待确认",
                "impact_level": "待确认",
                "mechanism": _mechanism("direct", name),
                "evidence": f"实时检索：东财公告接口{('·' + col) if col else ''}（未审核）",
                "source": "东方财富·公司公告",
                "source_url": ANN_DETAIL.format(code=code, art=art) if art else "",
                "review_status": "pending",
                "impact": "",
                "origin": "live",
            })
        return out, ("success" if out else "empty")

    items, status = _cached("ann", code, _do)
    return (items, status) if with_status else items


def fetch_news(code, name="", industry="", limit=8, timeout=8, profile=None, with_status=False):
    """市场快讯里提到该公司/主营/行业的条目。

    2026-11（P1）：不再一律 news_only。按证据强弱分三档：
      标题命中公司名          → unconfirmed（媒体提到 ≠ 政策依据）
      标题命中主营业务关键词  → business_related（关键词来自 F10 经营范围，可回溯）
      标题命中行业名/行业链    → industry_indirect
    三档都是待确认（direction/impact_level 不自动判方向），只是关联口径不同。
    """
    name = (name or "").strip()
    industry = (industry or "").strip()
    keywords = [k for k in ((profile or {}).get("keywords") or []) if len(k) >= 2]
    if not name and not industry and not keywords:
        return []

    def _do():
        try:
            import event_feed
            got = event_feed.fetch_headlines(limit=200)
        except Exception as e:
            return [], f"failed: {type(e).__name__}"
        # fetch_headlines 返回 (items, errors, ok_sources)
        if isinstance(got, tuple) and len(got) == 3:
            rows, errors, ok_sources = got
        else:
            rows, errors, ok_sources = got, [], 0
        total_sources = len(getattr(event_feed, "SOURCES", {}) or {}) or 1
        if ok_sources == 0:
            base_status = "failed"                       # 全部源失败 ≠ 没有相关政策
        elif ok_sources < total_sources:
            base_status = "partial"                      # 部分源失败
        else:
            base_status = None
        if not isinstance(rows, list) or not rows:
            return [], (base_status or "empty")

        # 公司名 / 主营关键词 / 行业名任一命中即收；只做字面匹配，不做同义词联想
        out = []
        for it in rows:
            title = (it.get("title") or "").strip()
            if not title:
                continue
            relation, why, matched = _classify_title(title, name, industry, keywords)
            if not relation:
                continue
            out.append({
                "event_id": f"live-news-{abs(hash(title)) % 10**10}",
                "title": title,
                "summary": "",
                "occurred_at": None,
                "published_at": _norm_date(it.get("published_at") or it.get("time")),
                "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "date_known": False,
                "date": None,
                "relation_type": relation,
                "relation_label": RELATION_LABEL.get(relation, "关联待确认"),
                "relation_basis": why,
                "direction": "待确认",
                "impact_level": "待确认",
                "mechanism": _mechanism(relation, matched),
                "evidence": f"实时检索：{why}（仅标题匹配，未核实原文，未审核）",
                "source": it.get("source") or "财经快讯",
                "source_url": it.get("url") or "",
                "review_status": "pending",
                "impact": "",
                "origin": "live",
            })
            if len(out) >= int(limit):
                break
        return out, (base_status or ("success" if out else "empty"))

    items, status = _cached("news", f"{code}|{name}|{industry}|{','.join(keywords)}", _do)
    return (items, status) if with_status else items


def _merge_status(ann_status, news_status):
    """把两个数据源的状态归并成 success / empty / partial / failed。

    规则（审查口径 2.4）：
      全部失败          → failed
      任一失败/部分失败  → partial
      都成功且有结果     → success
      都成功但没结果     → empty
    """
    states = [s for s in (ann_status, news_status) if s]
    failed = [s for s in states if str(s).startswith("failed")]
    partial = [s for s in states if s == "partial"]
    if states and len(failed) == len(states):
        return "failed"
    if failed or partial:
        return "partial"
    return "success" if any(s == "success" for s in states) else "empty"


def search_with_status(code, name="", industry="", limit=12, with_news=True, profile=None):
    """检索并返回 (items, status, detail)。status 见 _merge_status。

    detail 里保留每个源的原始结论，页面/日志能看出"到底哪一路挂了"。
    """
    prof = profile or {}
    if not industry:
        industry = prof.get("industry") or ""
    if not name:
        name = prof.get("name") or ""

    detail = {}
    try:
        ann, ann_status = fetch_announcements(code, name=name, limit=limit, with_status=True)
    except Exception as e:
        ann, ann_status = [], f"failed: {type(e).__name__}"
    detail["announcements"] = ann_status

    if with_news:
        try:
            news, news_status = fetch_news(code, name=name, industry=industry,
                                           limit=max(2, limit // 2), profile=prof,
                                           with_status=True)
        except Exception as e:
            news, news_status = [], f"failed: {type(e).__name__}"
    else:
        news, news_status = [], None
    if news_status:
        detail["news"] = news_status

    def norm_title(t):
        return re.sub(r"\s+", "", str(t or ""))

    seen, merged = set(), []
    for it in list(ann) + list(news):
        key = norm_title(it.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(it)

    out, order = [], sorted(merged, key=lambda it: RELATION_PRIORITY.get(it.get("relation_type"), 9))
    for rel in sorted({it.get("relation_type") for it in order},
                      key=lambda r: RELATION_PRIORITY.get(r, 9)):
        group = [it for it in order if it.get("relation_type") == rel]
        group.sort(key=lambda it: str(it.get("published_at") or ""), reverse=True)
        out.extend(group)

    status = _merge_status(ann_status, news_status)
    return out[:limit], status, detail


def search(code, name="", industry="", limit=12, with_news=True, profile=None):
    """合并两路结果并按关联强度排序：公告(direct) → 主营相关 → 行业间接 → 待确认。

    profile 来自 stock_profile：有经营范围关键词时，快讯才能判成 business_related；
    取不到画像就只按公司名/行业名匹配（口径降级，但不编造业务）。
    """
    prof = profile or {}
    if not industry:
        industry = prof.get("industry") or ""
    if not name:
        name = prof.get("name") or ""
    ann = fetch_announcements(code, name=name, limit=limit)
    news = (fetch_news(code, name=name, industry=industry, limit=max(2, limit // 2), profile=prof)
            if with_news else [])

    def norm_title(t):
        return re.sub(r"\s+", "", str(t or ""))

    seen, merged = set(), []
    for it in list(ann) + list(news):
        key = norm_title(it.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(it)

    out, order = [], sorted(merged, key=lambda it: RELATION_PRIORITY.get(it.get("relation_type"), 9))
    for rel in sorted({it.get("relation_type") for it in order},
                      key=lambda r: RELATION_PRIORITY.get(r, 9)):
        group = [it for it in order if it.get("relation_type") == rel]
        group.sort(key=lambda it: str(it.get("published_at") or ""), reverse=True)
        out.extend(group)
    return out[:limit]
