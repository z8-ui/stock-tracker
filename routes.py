"""
路由层 - URL 映射
"""

from flask import Blueprint, render_template, jsonify, request
from collections import defaultdict
from time import time
from datetime import datetime
import hmac
import re
from data_service import (
    get_market_flow, get_sector_flow,
    get_stock_money_flow, get_stock_valuation, search_stock,
    get_stock_quote, get_index_history, get_sector_valuation,
    get_stock_real_money_flow,
    estimate_intrinsic_value, INDUSTRY_PE_MAP, get_industry_pe,
    cache_meta, data_meta,
    _LAST_SUCCESS
)
# 技术分析纯函数独立模块（technical.py），与数据获取解耦
from technical import (
    calc_ema, calc_fibonacci_levels,
    get_kline, zigzag, support_resistance, find_trendlines
)
from chart_builder import (
    build_market_flow_chart, build_heatmap_chart,
    build_stock_flow_chart, build_valuation_chart
)
from config import APP_NAME, STOCK_WATCHLIST, VALUATION_WATCHLIST, DEEPSEEK_API_KEY

bp = Blueprint("main", __name__)


# ========== S3-3B：板块数据的 HTTP Headers 外露 ==========
# JSON 主体结构一个字不改（heatmap 仍是 chart list，sector-valuation 仍是
# {code, data, is_mock}）；三维度与行数信息走稳定的 X-Data-* 响应头，
# 这样浏览器端和脚本都能读到"这份数据完不完整、新不新鲜、从哪来的"。
#
# 编码约定（缺信息一律写 unknown，绝不默认 complete）：
#   X-Data-Fetch-Status  success / failed / not_attempted / unknown
#   X-Data-Completeness  complete / partial / unknown
#   X-Data-Freshness     fresh / stale / unknown
#   X-Data-Meta-Status   ok / missing / mismatch / orphan（元数据自检结果）
#   X-Data-Rows          int / unknown
#   X-Data-Total         int / unknown（上游没给总数就是 unknown）
#   X-Data-Fetched-At    "YYYY-MM-DD HH:MM:SS"（本地抓取时刻）/ unknown
#   X-Data-AsOf          上游数据时间；上游不提供时 unknown（不拿本地时间冒充）
#   X-Data-Served-From   network / memory / stale / file / file_fallback / none
#   X-Data-Missing       "3,5" 这样逗号分隔的缺失项；无缺失写 "-"
DATA_HEADER_NAMES = (
    "X-Data-Fetch-Status", "X-Data-Completeness", "X-Data-Freshness",
    "X-Data-Meta-Status", "X-Data-Rows", "X-Data-Total", "X-Data-Fetched-At",
    "X-Data-AsOf", "X-Data-Served-From", "X-Data-Missing",
)


def _staleness_from_meta(meta, cm):
    """JSON 的 is_stale 与 X-Data-Freshness 必须同源（S3-3B r1）。

    语义：is_stale = 本次返回的这份数据**不能被当作新鲜可用**。
      - 本次抓上游失败、退回旧缓存（file_fallback）→ stale，并给出明确原因；
      - 元数据可信但已过期 → stale；
      - 无法确认新鲜度（元数据缺失/不可信/没落盘）→ stale（不知道就不能说新鲜）；
      - 其余按 cache_meta 的年龄口径。
    返回 (is_stale, reason)，键名与旧 JSON 完全一致，只是取值不再与 Headers 矛盾。
    """
    cm = cm or {}
    reason = cm.get("reason") or ""
    if meta.get("served_from") == "file_fallback":
        return True, "上游抓取失败，返回旧缓存（本次未拿到新数据）"
    fresh = meta.get("freshness")
    if fresh == "stale":
        return True, reason or "缓存已过期"
    if fresh == "fresh":
        # r1b：以"本次实际返回数据的元数据"为准，不再回头看进程内 _LAST_SUCCESS。
        # 否则外部进程更新过缓存文件时会出现 freshness=fresh 却 is_stale=true。
        return False, ""
    return True, reason or "缺少可信元数据，无法确认新鲜度"


def _attach_data_headers(resp, meta):
    """把 data_meta() 的结果写进响应头（失败响应也写，方便前端区分"接口挂了"和"数据不全"）。"""
    meta = meta or {}
    missing = meta.get("missing") or []
    rows, total = meta.get("rows"), meta.get("total")
    resp.headers["X-Data-Fetch-Status"] = str(meta.get("fetch_status") or "unknown")
    resp.headers["X-Data-Completeness"] = str(meta.get("completeness") or "unknown")
    resp.headers["X-Data-Freshness"] = str(meta.get("freshness") or "unknown")
    resp.headers["X-Data-Meta-Status"] = str(meta.get("meta_status") or "missing")
    resp.headers["X-Data-Rows"] = "unknown" if rows is None else str(rows)
    resp.headers["X-Data-Total"] = "unknown" if total is None else str(total)
    resp.headers["X-Data-Fetched-At"] = meta.get("fetched_at_iso") or "unknown"
    resp.headers["X-Data-AsOf"] = str(meta.get("asof") or "unknown")
    resp.headers["X-Data-Served-From"] = str(meta.get("served_from") or "none")
    resp.headers["X-Data-Missing"] = ",".join(str(m) for m in missing) if missing else "-"
    # 跨域访问（前端 dev server / 局域网）时，浏览器默认读不到自定义头
    exposed = resp.headers.get("Access-Control-Expose-Headers") or ""
    names = [n for n in DATA_HEADER_NAMES if n not in exposed]
    if names:
        resp.headers["Access-Control-Expose-Headers"] = \
            ", ".join([x.strip() for x in exposed.split(",") if x.strip()] + names)
    return resp


# ========== AI 接口防护(限流 + 可选 token) ==========
# 目的: 防止 AI 分析接口被脚本循环调用刷爆 DeepSeek API 额度
# 限流: 每 IP 每分钟最多 AI_RATE_LIMIT 次(内存计数, 进程内有效)
# token: config 里 AI_API_TOKEN 非空时, 调用 /api/ai-analysis 必须带 Header `X-AI-Token`
#        ⚠️ 不再接受 ?token=xxx（URL 会落进历史/代理日志/access log），比较用 hmac.compare_digest
_ai_call_log = defaultdict(list)
AI_RATE_LIMIT = 10  # 次/分钟/IP


def _is_local_request():
    """请求是否来自本机回环。

    未配置 AI_API_TOKEN 时，AI 接口只允许本机调用：
    这个项目默认绑 127.0.0.1，但用户完全可能改 SERVER_HOST 给局域网用，
    那时"没配 token"就是任何人都能刷你的模型额度 —— 不能默认视为安全。
    """
    addr = (request.remote_addr or "").strip()
    if not addr:
        return False
    return addr == "localhost" or addr == "::1" or addr.startswith("127.")


def _ai_token_ok():
    """AI 接口访问策略（三层，从强到弱）：

    1. 配置了 AI_API_TOKEN → 只认 Header `X-AI-Token`，hmac.compare_digest 定长比较
    2. 没配 AI_API_TOKEN，但应用层开了局域网 Basic → Basic 凭据有效即放行
       （页面不再需要另存一个容易和全局认证冲突的 token）
    3. 都没有 → 仅允许本机（127.0.0.1/::1）；局域网/公网请求一律 403

    注意：第 3 条不是“认证”，只是“没有远程暴露”。给局域网使用时必须配 AI_API_TOKEN
    或 SERVER_AUTH_TOKEN。
    """
    from config import AI_API_TOKEN, SERVER_AUTH_TOKEN

    supplied = (request.headers.get("X-AI-Token") or "").strip()
    if AI_API_TOKEN:
        return bool(supplied) and hmac.compare_digest(supplied, AI_API_TOKEN)

    if SERVER_AUTH_TOKEN:
        auth = request.authorization
        try:
            if auth and hmac.compare_digest(auth.username or "", "stock") \
                    and hmac.compare_digest(auth.password or "", SERVER_AUTH_TOKEN):
                return True
        except Exception:
            pass

    return _is_local_request()


def _ai_rate_limited():
    """内存限流, 返回 True 表示超限"""
    ip = request.remote_addr or "unknown"
    now = time()
    _ai_call_log[ip] = [t for t in _ai_call_log[ip] if now - t < 60]
    if len(_ai_call_log[ip]) >= AI_RATE_LIMIT:
        return True
    _ai_call_log[ip].append(now)
    return False


# ========== 页面路由 ==========

@bp.route("/")
def dashboard():
    return render_template("dashboard.html", app_name=APP_NAME)

@bp.route("/market-flow")
def market_flow_page():
    return render_template("market_flow.html", app_name=APP_NAME)

@bp.route("/heatmap")
def heatmap_page():
    return render_template("heatmap.html", app_name=APP_NAME)

@bp.route("/stock-flow")
def stock_flow_page():
    return render_template("stock_flow.html", app_name=APP_NAME, watchlist=STOCK_WATCHLIST)

@bp.route("/valuation")
def valuation_page():
    return render_template("valuation.html", app_name=APP_NAME, watchlist=VALUATION_WATCHLIST)


# ========== 数据接口 ==========

@bp.route("/api/health")
def api_health():
    """存活探针：进程活着就 200（即使数据降级）

    额外回显交易日历状态：configured=False 表示没有节假日表，节假日会被当交易日。
    """
    from data_service import calendar_info
    return jsonify({"code": 200, "status": "ok",
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "calendar": calendar_info()})


@bp.route("/api/ready")
def api_ready():
    """就绪探针：关键数据硬过期时 503（用于部署/监控，本地可忽略）
    判断标准：所有已构建数据距最后成功超过 READY_MAX_AGE 秒 → 不 ready
    """
    READY_MAX_AGE = 12 * 3600  # 12 小时硬过期
    now = time()
    if not _LAST_SUCCESS:
        # 刚启动还没构建过 → 允许预热期（health 200 即可）
        return jsonify({"code": 200, "ready": True, "msg": "预热中（尚无数据构建记录）"})
    stale = [k for k, ts in _LAST_SUCCESS.items() if now - ts > READY_MAX_AGE]
    if len(stale) == len(_LAST_SUCCESS):
        return jsonify({"code": 503, "ready": False,
                        "msg": "关键数据硬过期", "stale_keys": stale[:5]}), 503
    return jsonify({"code": 200, "ready": True,
                    "last_success_count": len(_LAST_SUCCESS),
                    "stale_count": len(stale)})


@bp.route("/api/market-flow")
def api_market_flow():
    data = get_market_flow()
    chart = build_market_flow_chart(data)
    if chart:
        resp = {"code": 200, "data": chart}
        if data and "_market_status" in data:
            resp["_market_status"] = data["_market_status"]
        if isinstance(data, dict):
            resp["is_mock"] = bool(data.get("is_mock"))
        meta = cache_meta("market_flow")
        # 数据自带 _stale（dict 型）时以它为准；格式化后的 asof 给前端显示
        resp["is_stale"] = bool((data or {}).get("_stale")) if isinstance(data, dict) and "_stale" in (data or {}) else meta["is_stale"]
        resp["asof"] = meta["asof"]
        return jsonify(resp)
    return jsonify({"code": 500, "msg": "数据获取失败（非交易日或接口限制）"})


@bp.route("/api/heatmap")
def api_heatmap():
    trace = {}
    sectors = get_sector_flow(trace=trace)
    chart = build_heatmap_chart(sectors)
    meta = data_meta("sector_flow", served_from=trace.get("served"), trace=trace,
                     data=sectors)
    if chart:
        # 附加市场状态
        from data_service import _market_status as _ms
        status, _ = _ms()
        is_mock = any(bool(s.get("is_mock")) for s in (sectors or []) if isinstance(s, dict))
        cm = cache_meta("sector_flow")
        is_stale, stale_reason = _staleness_from_meta(meta, cm)
        # asof 与 freshness 同源：外部进程更新过缓存文件时 _LAST_SUCCESS 可能还是旧的
        asof = cm.get("asof")
        if meta.get("freshness") == "fresh":
            asof = meta.get("fetched_at_iso") or asof
        resp = jsonify({"code": 200, "data": chart, "_market_status": status,
                        "is_mock": is_mock, "is_stale": is_stale,
                        "asof": asof, "stale_reason": stale_reason})
        return _attach_data_headers(resp, meta)
    if sectors is None:
        resp = jsonify({"code": 500, "msg": "暂无板块资金数据（接口未返回，未启用模拟兜底）"})
        return _attach_data_headers(resp, meta)
    resp = jsonify({"code": 500, "msg": "数据获取失败"})
    return _attach_data_headers(resp, meta)


@bp.route("/api/stock-quote")
def api_stock_quote():
    """个股实时行情"""
    code = request.args.get("code", "600519")
    data = get_stock_quote(code)
    if data:
        return jsonify({"code": 200, "data": data})
    return jsonify({"code": 500, "msg": "数据获取失败"})


@bp.route("/api/stock-flow")
def api_stock_flow():
    """个股资金流向"""
    code = request.args.get("code", "600519")
    name = request.args.get("name", "未知")
    data = get_stock_money_flow(code)
    chart = build_stock_flow_chart(name, data)
    if chart:
        return jsonify({"code": 200, "data": chart})
    return jsonify({"code": 500, "msg": "暂无资金流数据（东财逐日资金流接口未返回，非随机填充）"})


@bp.route("/api/stock-real-flow")
def api_stock_real_flow():
    """个股当日真实资金净流向 + 占比（大单净量等）"""
    code = request.args.get("code", "600519")
    data = get_stock_real_money_flow(code)
    if data:
        return jsonify({"code": 200, "data": data})
    return jsonify({"code": 500, "msg": "获取资金流数据失败"})


@bp.route("/api/valuation")
def api_valuation():
    """个股估值（refresh=1 时强制穿透行业PE/财报缓存重新拉取）"""
    code = request.args.get("code", "600519")
    name = request.args.get("name", "未知")
    refresh = request.args.get("refresh", "") == "1"
    data = get_stock_valuation(code, refresh=refresh)
    # 动态行业 PE（东财板块实时，失败降级静态映射表）
    ind = get_industry_pe(code, refresh=refresh)
    chart = build_valuation_chart(name, data, industry_pe=ind["pe"])
    if chart:
        chart["industry_name"] = ind.get("industry", "未知")
        chart["industry_pe_source"] = ind.get("source", "unknown")
        # 静态行业 PE 是人工估算，前端必须能标"参考值"，不能和东财实时值长得一样
        chart["industry_pe_source_label"] = ind.get("source_label", "")
        chart["industry_pe_is_static"] = bool(ind.get("is_static"))
        chart["industry_pe_asof"] = ind.get("_asof")
        # 透明化（P1-4）：行情时间/来源、财报期、偏离口径说明，全部随响应返回
        chart["quote_asof"] = data.get("_asof")
        chart["quote_source"] = data.get("_source", "")
        chart["quote_stale"] = bool(data.get("_stale"))
        chart["quote_stale_reason"] = data.get("_stale_reason", "")
        chart["deviation_basis"] = (
            "偏离度 = (个股PE − 行业PE) / 行业PE；两端均为 PE(TTM) 口径，"
            "仅作横向参考，不直接等于高估/低估结论")
        # 2026-08: 财报快照 + 政策事件（build_valuation_chart 会重建 dict，需手动补挂）
        chart["financials"] = data.get("financials")
        # S3-4B：请求级财报状态跟着这次响应走，不读全局最新值
        chart["financials_status"] = data.get("financials_status")
        chart["policy_notes"] = data.get("policy_notes") or []
        return jsonify({"code": 200, "data": chart})
    return jsonify({"code": 500, "msg": "数据获取失败"})


@bp.route("/api/search-stock")
def api_search_stock():
    keyword = request.args.get("q", "")
    if not keyword:
        return jsonify({"code": 400, "msg": "请输入关键词"})
    result = search_stock(keyword)
    return jsonify({"code": 200, "data": result or []})


@bp.route("/ai")
def ai_page():
    """AI 分析页面"""
    return render_template("ai.html", app_name=APP_NAME)


@bp.route("/api/ai-config")
def api_ai_config():
    """AI 配置状态(供页面判断是否需要显示 API Key 填写框)
    只返回是否已配置, 绝不返回 key 本身
    """
    from config import AI_API_TOKEN
    return jsonify({
        "code": 200,
        "data": {
            "hasKey": bool((DEEPSEEK_API_KEY or "").strip()),
            "applyUrl": "https://platform.deepseek.com",
            # 明确访问策略：配了 token = 只认 Header；没配 = 仅本机可用
            "tokenConfigured": bool(AI_API_TOKEN),
            "tokenPolicy": "header" if AI_API_TOKEN else "local_only",
            "tokenHint": ("请求头 X-AI-Token" if AI_API_TOKEN else
                          "未配置 AI_API_TOKEN：AI 接口仅允许本机访问，局域网使用必须在 config_local.py 配置"),
        }
    })


@bp.route("/api/ai-analysis")
def api_ai_analysis():
    """AI 技术分析: 拉K线 -> 算指标 -> DeepSeek 生成分析"""
    if not _ai_token_ok():
        return jsonify({"code": 403, "msg": "AI 接口需要认证：请通过应用认证访问，或携带有效的 X-AI-Token"}), 403
    if _ai_rate_limited():
        return jsonify({"code": 429, "msg": f"调用过于频繁, 限流 {AI_RATE_LIMIT} 次/分钟, 请稍后再试"}), 429
    code = request.args.get("code", "600519")
    name = request.args.get("name", "贵州茅台")
    market = request.args.get("market", "")
    refresh = request.args.get("refresh", "") == "1"
    # 页面传入的临时 key 只从 Header 取，不再接受 ?api_key=
    # （URL 会落进浏览器历史 / 代理日志 / Flask access log，等于把 key 写进一堆明文文件）
    api_key = (request.headers.get("X-AI-Key") or "").strip()
    if market == "us":
        return jsonify({"code": 400, "msg": "美股暂不支持 AI 技术分析（K线数据源限制），可搜索 A股/港股/指数/基金"})
    from ai_analysis import analyze
    result = analyze(code, name, market or None, refresh=refresh, api_key=api_key or None)
    if not result:
        return jsonify({"code": 500, "msg": "K线数据不足(可能休市、代码错误或该品种暂不支持)"})
    analysis = (result.get("analysis") or "").strip()
    if not analysis:
        return jsonify({"code": 502, "msg": "AI 返回空内容，未写入缓存"}), 502
    # AI 调用失败/未配置 key：明确返回 502，而不是 200 里塞一段"AI 分析调用失败"文本
    # （前端和监控都看不出这是错误；而且错误流不该伪装成正常结果）
    if analysis.startswith(("AI 分析调用失败", "未配置")):
        return jsonify({"code": 502, "msg": analysis[:300]}), 502
    return jsonify({"code": 200, "data": result})


@bp.route("/api/index-analysis")
def api_index_analysis():
    """指数技术分析（EMA20偏离度、斐波那契）"""
    code = request.args.get("code", "000688")
    name = request.args.get("name", "科创50")
    klines = get_index_history(code, 40)
    if not klines:
        return jsonify({"code": 500, "msg": "获取指数数据失败"})
    
    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    
    # EMA20
    ema20 = calc_ema(closes, 20)
    
    # 偏离度 = (收盘价 - EMA20) / EMA20 * 100
    deviation = []
    for i, (c, e) in enumerate(zip(closes, ema20)):
        if e and e > 0:
            deviation.append(round((c - e) / e * 100, 2))
        else:
            deviation.append(None)
    
    # 斐波那契（传入最新收盘价做自适应展宽）
    recent_high = max(highs[-10:])
    recent_low = min(lows[-10:])
    fib = calc_fibonacci_levels(recent_high, recent_low, price=closes[-1] if closes else None)
    
    return jsonify({
        "code": 200,
        "data": {
            "index_name": name,
            "dates": [k["date"] for k in klines],
            "closes": closes,
            "ema20": ema20,
            "deviation": deviation,
            "fibonacci": fib,
            "is_mock": bool(any(k.get("is_mock") for k in klines)),
            "asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "tencent"
        }
    })


@bp.route("/api/swing-analysis")
def api_swing_analysis():
    """摆动点 · 支撑压力 · 趋势线（画线程序化，支持日线/周线级别）"""
    code = request.args.get("code", "600519")
    name = request.args.get("name", "")
    level = request.args.get("level", "day")   # day / week
    freq = "week" if level == "week" else "day"

    klines = get_kline(code, days=90 if freq == "day" else 80, freq=freq)
    if not klines or len(klines) < 10:
        return jsonify({"code": 500, "msg": "K线数据不足（可能休市或代码错误）"})

    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]

    # 级别越大摆动阈值越大（过滤小波动，只看大结构）
    threshold = 0.03 if freq == "day" else 0.05
    swings = zigzag(highs, lows, threshold_pct=threshold)
    sr = support_resistance(swings, tol_pct=0.03)
    trendlines = find_trendlines(swings)

    return jsonify({
        "code": 200,
        "data": {
            "name": name,
            "level": level,
            "threshold_pct": threshold,
            "dates": [k["date"] for k in klines],
            "klines": klines,                       # 完整 OHLC 供画 K 线
            "swing_points": swings,
            "support_resistance": sr,
            "trendlines": trendlines,
            "current_price": closes[-1],
            "asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "tencent"
        }
    })


@bp.route("/api/sector-valuation")
def api_sector_valuation():
    """板块估值数据（JSON 结构不变；三维度与行数走 X-Data-* 响应头）"""
    trace = {}
    data = get_sector_valuation(trace=trace)
    meta = data_meta("sector_valuation", served_from=trace.get("served"), trace=trace,
                     data=data)
    if data:
        is_mock = any(bool(s.get("is_mock")) for s in data if isinstance(s, dict))
        resp = jsonify({"code": 200, "data": data, "is_mock": is_mock})
        return _attach_data_headers(resp, meta)
    resp = jsonify({"code": 500, "msg": "获取板块估值失败（接口未返回，未启用模拟兜底）"})
    return _attach_data_headers(resp, meta)


@bp.route("/api/intrinsic-value")
def api_intrinsic_value():
    """个股内在价值分析"""
    code = request.args.get("code", "600519")
    quote = get_stock_quote(code)
    if not quote:
        return jsonify({"code": 500, "msg": "获取数据失败"})
    iv = estimate_intrinsic_value(quote)
    # estimate_intrinsic_value 内部为自己的请求生成了状态，这里只是原样带出
    return jsonify({"code": 200, "data": iv})


@bp.route("/api/fibonacci")
def api_fibonacci():
    """个股斐波那契回调线（用于估值偏离界面）
    使用真实K线数据计算近20日斐波那契回调位
    """
    code = request.args.get("code", "600519")
    name = request.args.get("name", "")
    
    # 获取实时行情校准当前价
    quote = get_stock_quote(code)
    if not quote:
        return jsonify({"code": 500, "msg": "获取个股数据失败"})
    
    # 获取个股真实K线数据（统一走 data_service.get_kline，避免重复实现）
    klines = get_kline(code, days=25, freq="day")
    if not klines or len(klines) < 5:
        return jsonify({"code": 500, "msg": "K线数据不足"})

    closes = [k["close"] for k in klines]
    highs_v = [k["high"] for k in klines]
    lows_v = [k["low"] for k in klines]

    # 用实时行情替换最后一个收盘价（保证当前价准确）
    current_price = quote["price"]
    if closes:
        closes[-1] = current_price

    # 近20日高低点计算斐波那契（传入当前价做自适应展宽）
    recent_high = max(highs_v[-20:])
    recent_low = min(lows_v[-20:])
    fib = calc_fibonacci_levels(recent_high, recent_low, price=current_price)

    dates = [k["date"] for k in klines]
    # 找到当前价最接近的斐波那契位
    nearest_level = None
    nearest_diff = float("inf")
    for k, v in fib["levels"].items():
        diff = abs(current_price - v)
        if diff < nearest_diff:
            nearest_diff = diff
            nearest_level = k
    return jsonify({
        "code": 200,
        "data": {
            "name": name,
            "current_price": current_price,
            "fibonacci": fib,
            "dates": dates,
            "closes": closes,
            "nearest_level": nearest_level,
            "deviation_pct": round((current_price - fib["levels"][nearest_level]) / fib["levels"][nearest_level] * 100, 2) if nearest_level else 0,
            "asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "tencent"
        }
    })


# ---------------------------------------------------------------- 政策事件中心（已下线）
# 2026-11-07 按用户要求删除独立的「政策事件」页面：导航入口、/events 页面路由、
# /api/event-timeline 接口，以及 templates/events.html、static/js/events.js 一并移除。
# 事件数据层（events_store / event_feed / scripts/events_cli.py）保留 —— 个股估值页的
# 政策区块和每日采集流程仍在用，删了会连带坏掉。


@bp.route("/api/data-health")
def api_data_health():
    """数据健康快照：只读缓存与事件库，不发任何外部请求（审查 3.3）。

    单模块异常不会让整个接口 500 —— 返回哪块坏了、坏在哪。
    """
    try:
        import data_health
        snap = data_health.snapshot()
    except Exception as e:
        return jsonify({"code": 500, "msg": f"数据健康统计失败: {type(e).__name__}: {str(e)[:120]}"}), 500
    return jsonify({"code": 200, "data": snap})


@bp.route("/api/policy-events")
def api_policy_events():
    """按个股检索政策与事件（关系类型 / 影响程度 / 机制 / 证据 / 审核状态）。

    参数：stock_code（必填，5-6 位数字）、stock_name（仅回显）、industry（可选，
    缺省时按代码查一次行业；查不到就只返回个股直连事件，不按同行业硬凑）。

    data_status：
      real        库正常且有关联事件
      empty       库正常但这只股票确实没有已关联的事件（前端显示"暂无"）
      unavailable 事件库不可用（没有表/打不开）—— 和 empty 必须分开，
                  否则"库挂了"会被显示成"这只股票没有政策"，静默误导用户

    live=1 时额外实时检索公开材料（公司公告 + 快讯标题命中）：
      这些条目 origin='live'、review_status='pending'，只作线索，不是结论；
      抓不到就静默跳过，不让外网故障影响已入库事件的展示。
    """
    code = (request.args.get("stock_code") or request.args.get("code") or "").strip()
    if not re.fullmatch(r"\d{5,6}", code):
        return jsonify({"code": 400, "msg": "stock_code 必须是 5-6 位数字代码"}), 400
    name = (request.args.get("stock_name") or "").strip()[:40]
    industry = (request.args.get("industry") or "").strip()[:40]
    live = request.args.get("live", "") == "1"
    refresh = request.args.get("refresh") == "1"

    from datetime import datetime
    meta = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}  # type: dict
    profile = {}

    # ---- 0. 个股画像（仅 live）：行业 / 主营 / 关键词 ----
    # 必须放在库查询之前：库内事件是按 (代码, 行业) 关联的，行业为空会直接漏掉
    # 行业/产业链政策。旧实现先查库、后取行业，live 请求永远拿不到行业事件。
    if live:
        try:
            import stock_profile
            if refresh:
                stock_profile.clear_cache()
            profile = stock_profile.profile_for(code, industry=industry, refresh=refresh)
            meta["profile_status"] = "ok" if not profile.get("missing") else "partial"
            for m in profile.get("missing") or []:
                meta.setdefault("profile_missing", []).append(m)
        except Exception as e:
            profile = {}
            meta["profile_status"] = f"failed: {type(e).__name__}"

    # ---- 1. 行业名：请求参数 → 东财行业 → 画像行业（便宜的在前，贵的在后）----
    if not industry:
        try:
            industry = (get_industry_pe(code).get("industry") or "")
        except Exception:
            industry = ""
    if not industry:
        industry = profile.get("industry") or ""
    meta["industry_source"] = ("request" if (request.args.get("industry") or "").strip()
                               else ("eastmoney" if industry else "none"))

    # ---- 2. 库内已关联事件（现在 industry 一定有值或明确为空）----
    try:
        import events_store
        # 先把表建好：新装环境里"表存在但没数据"应报 empty，
        # 而不是把"还没初始化"显示成"事件库不可用"。
        events_store.init_db()
        events = events_store.policy_events_for(code, stock_name=name, industry_name=industry)
        # 东财板块行业（工业金属）和 F10 行业链末级（黄金）是两套分类，
        # 只查一个会漏掉另一个分类下的行业政策 —— 两个都查，按标题去重合并。
        tail = (profile.get("industry_chain") or [""])[-1] if profile else ""
        meta["industry_aliases"] = [industry] + ([tail] if tail and tail != industry else [])
        if tail and tail != industry:
            seen_titles = {e.get("title") for e in (events or [])}
            for ev in events_store.policy_events_for(code, stock_name=name,
                                                     industry_name=tail) or []:
                if ev.get("title") not in seen_titles:
                    seen_titles.add(ev.get("title"))
                    events.append(ev)
    except Exception as e:
        return jsonify({"code": 500, "msg": f"事件库读取失败: {type(e).__name__}: {e}"}), 500

    if events is None:
        meta.update({"total": 0, "data_status": "unavailable"})
        events = []
    else:
        meta.update({"total": len(events), "data_status": "real" if events else "empty"})

    # ---- 3. 实时检索：成功/空/部分失败/全部失败 必须分开 ----
    if live:
        live_items = []
        try:
            import policy_live
            if refresh:
                policy_live.clear_cache()
            live_items, live_status, live_detail = policy_live.search_with_status(
                code, name, industry, limit=8, profile=profile)
            meta["live_status"] = live_status          # success / empty / partial / failed
            meta["live_status_detail"] = live_detail
        except Exception as e:
            meta["live_status"] = "failed"
            meta["live_status_detail"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
        known = {e.get("title") for e in events}
        added = [x for x in live_items if x.get("title") not in known]
        events = events + added
        meta["live_total"] = len(added)
        if added and meta.get("data_status") in ("empty", "unavailable"):
            meta["data_status"] = "real"
        meta["relation_counts"] = {}
        for ev in events:
            rel = ev.get("relation_type") or "unconfirmed"
            meta["relation_counts"][rel] = meta["relation_counts"].get(rel, 0) + 1

    return jsonify({"code": 200, "data": {
        "stock_code": code,
        "stock_name": name or (profile.get("name") if live else "") or "",
        "industry": industry,
        "profile": profile if live else {},
        "events": events,
        "meta": meta,
    }})
