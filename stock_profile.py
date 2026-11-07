"""个股画像：行业 / 主营业务 / 产业链位置 / 检索关键词（V2.0.1 P1）

为什么单独一个模块：政策事件要"按个股"关联，就必须先知道这家公司做什么。
之前只能拿行业名硬凑，等于把"同行业所有公司业务相同"当成事实 —— 审查点名禁止。

数据来源（只认能验证的）：
  东方财富 F10「公司概况」接口
    EM2016        行业三级分类，如 食品饮料-饮料-白酒 → 产业链位置
    BUSINESS_SCOPE 经营范围 / 主营业务原文
    ORG_PROFILE    公司简介
拿不到就**明确标记缺失**（missing 列表），绝不凭空生成主营业务；
关键词全部从经营范围原文里切出来，可回溯到来源，不做同义词联想。

缓存 6 小时：公司概况基本不变，但重开页面不该每次都打外网。
"""

import re
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
F10_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={secid}"
DETAIL_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/Index?type=web&code={secid}"
TTL = 6 * 3600
_CACHE = {}

# 经营范围里这些词单独拿出来当检索词没有意义（会命中一堆无关新闻）
_STOPWORDS = {
    "生产", "销售", "生产与销售", "开发", "经营", "服务", "管理", "技术开发",
    "技术咨询", "技术服务", "进出口", "货物进出口", "技术进出口", "其他",
    "具体内容以工商核定登记为准", "依法须经批准的项目", "经相关部门批准后方可开展经营活动",
}
_SPLIT_RE = re.compile(r"[、；;，,。()（）/\s]+")


def _secid(code):
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return None
    return ("SH" if code.startswith(("6", "9")) else "SZ") + code


# 连接词：经营范围里常用它把两件事拼成一个长句（"锂离子电池以及其他电池"），
# 不切开就会被当成一个永远匹配不到的"关键词"。
_CONNECTOR_RE = re.compile(r"以及|及其|等相关|等业务|等产品|等|包括|并且|同时|主要有|从事|(?<![涉普波])及(?=..)")
# 只有从句首/句尾剥掉，不在中间切 —— 中间切会把"比亚迪"这种词切坏
_EDGE = " 　的与和及、，,。；;：:()（）"
_GENERIC = {
    "其他", "其它", "各种", "各类", "所有", "上述", "以上", "其他电池",
    "产品", "业务", "服务", "技术", "项目", "经营", "生产", "销售", "研发",
    "国家", "法律", "法规", "许可", "批准", "一般", "依法", "须经",
}
_DROP_PREFIX = ("其他", "其它", "各种", "各类", "所有", "上述", "以上")


def _terms_with_evidence(text, limit=12):
    """从经营范围切出关键词，并保留每个词的原文出处。

    规则（2026-11 修订）：
      1. 先去括号注释 —— 括号里多是"依法须经批准的项目"这类套话；
      2. 按连接词切开，避免"锂离子电池以及其他电池"这种整句当词；
      3. 每条关键词都回指它出现的那一小句，页面上可以核对；
      4. 不扩同义词、不做联想 —— 匹配不到的宁可没有，也不许编。
    """
    src = re.sub(r"[（(][^）)]*[）)]", "", str(text or ""))
    out, evidence, seen = [], [], set()
    for clause in _SPLIT_RE.split(src):
        clause = clause.strip(_EDGE)
        if not clause:
            continue
        for raw in _CONNECTOR_RE.split(clause):
            term = raw.strip(_EDGE)
            if "的" in term and len(term) > 6:
                term = term.split("的")[0].strip(_EDGE)   # "系列酒的生产与销售" → "系列酒"
            if len(term) < 2 or len(term) > 14:
                continue
            if term in _GENERIC or term in seen:
                continue
            if term.startswith(_DROP_PREFIX):
                continue
            if re.search(r"(其他|其它|各种|各类|所有)$", term):
                continue
            seen.add(term)
            out.append(term)
            evidence.append({"keyword": term, "source": "经营范围", "text": clause})
            if len(out) >= limit:
                return out, evidence
    return out, evidence


def _split_terms(text, limit=12):
    return _terms_with_evidence(text, limit=limit)[0]


def empty_profile(code, industry="", reason="未获取到公司概况"):
    """取不到就如实标缺失 —— 调用方据此在页面上写"资料缺失"，而不是编一段主营业务。"""
    return {
        "code": str(code or ""),
        "name": "",
        "industry": industry or "",
        "industry_chain": [],
        "business": "",
        "business_short": "",
        "keywords": [k for k in (industry,) if k],
        "keyword_evidence": [],
        "source": "",
        "source_url": "",
        "asof": time.strftime("%Y-%m-%d"),
        "missing": ["business", "industry_chain"],
        "note": reason,
    }


def fetch_profile(code, timeout=6):
    """抓 F10 公司概况并整理成画像；任何异常都降级成 empty_profile（不抛）。"""
    secid = _secid(code)
    if not secid:
        return empty_profile(code, reason="股票代码不合法")

    try:
        r = requests.get(F10_URL.format(secid=secid),
                         headers={"User-Agent": UA, "Referer": "https://emweb.securities.eastmoney.com/"},
                         timeout=timeout)
        if r.status_code != 200:
            return empty_profile(code, reason=f"F10 接口 HTTP {r.status_code}")
        data = r.json() or {}
    except Exception as e:                      # 网络/解析失败一律降级，不让页面挂
        return empty_profile(code, reason=f"F10 接口异常：{type(e).__name__}")

    rows = data.get("jbzl") or []
    if not isinstance(rows, list) or not rows:
        return empty_profile(code, reason="F10 未返回公司概况")
    jb = rows[0] if isinstance(rows[0], dict) else {}

    chain = [s for s in str(jb.get("EM2016") or "").split("-") if s]
    business = str(jb.get("BUSINESS_SCOPE") or "").strip()
    profile_text = str(jb.get("ORG_PROFILE") or "").strip()

    # 主营业务原文优先用经营范围；没有就退到公司简介第一句（并标明来源不同）
    business_src = "经营范围"
    if not business and profile_text:
        business = profile_text.split("。")[0]
        business_src = "公司简介"

    missing = []
    if not business:
        missing.append("business")
    if not chain:
        missing.append("industry_chain")

    # 关键词 = 经营范围切词 + 行业链末级（都是可回溯的字面词，不做联想）
    keywords, kw_evidence = _terms_with_evidence(business, limit=12)
    if chain:
        tail = chain[-1]
        if tail and tail not in keywords:
            keywords.insert(0, tail)
            kw_evidence.insert(0, {"keyword": tail, "source": "行业分类",
                                   "text": " > ".join(chain)})

    return {
        "code": jb.get("SECURITY_CODE") or str(code),
        "name": jb.get("SECURITY_NAME_ABBR") or jb.get("ORG_NAME") or "",
        "industry": chain[-1] if chain else "",
        "industry_chain": chain,
        "business": business,
        "business_short": (business[:120] + "…") if len(business) > 120 else business,
        "business_source": business_src,
        "keywords": keywords[:12],
        "keyword_evidence": kw_evidence[:12],
        "source": "东方财富 F10·公司概况",
        "source_url": DETAIL_URL.format(secid=secid),
        "asof": time.strftime("%Y-%m-%d"),
        "missing": missing,
        "note": "" if not missing else "公司概况缺项：" + "、".join(missing),
    }


def profile_for(code, industry="", refresh=False):
    """带缓存入口：同一只股票 6 小时内只打一次外网。"""
    key = str(code or "") + "|" + str(industry or "")
    now = time.time()
    hit = _CACHE.get(key)
    if hit and not refresh and now - hit[0] < TTL:
        return hit[1]
    prof = fetch_profile(code)
    if not prof.get("industry") and industry:
        prof["industry"] = industry
    if industry and industry not in prof.get("keywords", []):
        prof.setdefault("keywords", []).insert(0, industry)
    _CACHE[key] = (now, prof)
    return prof


def clear_cache():
    _CACHE.clear()
