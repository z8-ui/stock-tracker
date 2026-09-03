"""
路由层 - URL 映射
"""

from flask import Blueprint, render_template, jsonify, request
from collections import defaultdict
from time import time
from datetime import datetime
from data_service import (
    get_market_flow, get_sector_flow,
    get_stock_money_flow, get_stock_valuation, search_stock,
    get_stock_quote, get_index_history, get_sector_valuation,
    get_stock_real_money_flow,
    estimate_intrinsic_value, INDUSTRY_PE_MAP, get_industry_pe,
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
from config import APP_NAME, STOCK_WATCHLIST, VALUATION_WATCHLIST

bp = Blueprint("main", __name__)


# ========== AI 接口防护(限流 + 可选 token) ==========
# 目的: 防止 AI 分析接口被脚本循环调用刷爆 DeepSeek API 额度
# 限流: 每 IP 每分钟最多 AI_RATE_LIMIT 次(内存计数, 进程内有效)
# token: config 里 AI_API_TOKEN 非空时, 调用 /api/ai-analysis 必须带 ?token=xxx
_ai_call_log = defaultdict(list)
AI_RATE_LIMIT = 10  # 次/分钟/IP


def _ai_token_ok():
    """可选 token 校验: AI_API_TOKEN 为空则不校验(默认)"""
    from config import AI_API_TOKEN
    if not AI_API_TOKEN:
        return True
    return request.args.get("token") == AI_API_TOKEN


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
    """存活探针：进程活着就 200（即使数据降级）"""
    return jsonify({"code": 200, "status": "ok", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


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
        return jsonify(resp)
    return jsonify({"code": 500, "msg": "数据获取失败（非交易日或接口限制）"})


@bp.route("/api/heatmap")
def api_heatmap():
    sectors = get_sector_flow()
    chart = build_heatmap_chart(sectors)
    if chart:
        # 附加市场状态
        from data_service import _market_status as _ms
        status, _ = _ms()
        return jsonify({"code": 200, "data": chart, "_market_status": status})
    return jsonify({"code": 500, "msg": "数据获取失败"})


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
    return jsonify({"code": 500, "msg": "非交易日无详细资金流数据"})


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
        # 2026-08: 财报快照 + 政策事件（build_valuation_chart 会重建 dict，需手动补挂）
        chart["financials"] = data.get("financials")
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


@bp.route("/api/ai-analysis")
def api_ai_analysis():
    """AI 技术分析: 拉K线 -> 算指标 -> DeepSeek 生成分析"""
    if not _ai_token_ok():
        return jsonify({"code": 403, "msg": "token 错误"})
    if _ai_rate_limited():
        return jsonify({"code": 429, "msg": f"调用过于频繁, 限流 {AI_RATE_LIMIT} 次/分钟, 请稍后再试"})
    code = request.args.get("code", "600519")
    name = request.args.get("name", "贵州茅台")
    market = request.args.get("market", "")
    refresh = request.args.get("refresh", "") == "1"
    if market == "us":
        return jsonify({"code": 400, "msg": "美股暂不支持 AI 技术分析（K线数据源限制），可搜索 A股/港股/指数/基金"})
    from ai_analysis import analyze
    result = analyze(code, name, market or None, refresh=refresh)
    if not result:
        return jsonify({"code": 500, "msg": "K线数据不足(可能休市、代码错误或该品种暂不支持)"})
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
    """板块估值数据"""
    data = get_sector_valuation()
    if data:
        return jsonify({"code": 200, "data": data})
    return jsonify({"code": 500, "msg": "获取板块估值失败"})


@bp.route("/api/intrinsic-value")
def api_intrinsic_value():
    """个股内在价值分析"""
    code = request.args.get("code", "600519")
    quote = get_stock_quote(code)
    if not quote:
        return jsonify({"code": 500, "msg": "获取数据失败"})
    iv = estimate_intrinsic_value(quote)
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
