"""
路由层 - URL 映射
"""

from flask import Blueprint, render_template, jsonify, request
from data_service import (
    get_market_flow, get_sector_flow,
    get_stock_money_flow, get_stock_valuation, search_stock,
    get_stock_quote, get_index_history, get_sector_valuation,
    calc_ema, calc_fibonacci_levels, estimate_intrinsic_value
)
from chart_builder import (
    build_market_flow_chart, build_heatmap_chart,
    build_stock_flow_chart, build_valuation_chart
)
from config import APP_NAME, STOCK_WATCHLIST, VALUATION_WATCHLIST

bp = Blueprint("main", __name__)


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

@bp.route("/api/market-flow")
def api_market_flow():
    data = get_market_flow()
    chart = build_market_flow_chart(data)
    if chart:
        return jsonify({"code": 200, "data": chart})
    return jsonify({"code": 500, "msg": "数据获取失败（非交易日或接口限制）"})


@bp.route("/api/heatmap")
def api_heatmap():
    sectors = get_sector_flow()
    chart = build_heatmap_chart(sectors)
    if chart:
        return jsonify({"code": 200, "data": chart})
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


@bp.route("/api/valuation")
def api_valuation():
    """个股估值"""
    code = request.args.get("code", "600519")
    name = request.args.get("name", "未知")
    data = get_stock_valuation(code)
    chart = build_valuation_chart(name, data)
    if chart:
        return jsonify({"code": 200, "data": chart})
    return jsonify({"code": 500, "msg": "数据获取失败"})


@bp.route("/api/search-stock")
def api_search_stock():
    keyword = request.args.get("q", "")
    if not keyword:
        return jsonify({"code": 400, "msg": "请输入关键词"})
    result = search_stock(keyword)
    return jsonify({"code": 200, "data": result or []})


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
    
    # 斐波那契
    recent_high = max(highs[-10:])
    recent_low = min(lows[-10:])
    fib = calc_fibonacci_levels(recent_high, recent_low)
    
    return jsonify({
        "code": 200,
        "data": {
            "index_name": name,
            "dates": [k["date"] for k in klines],
            "closes": closes,
            "ema20": ema20,
            "deviation": deviation,
            "fibonacci": fib
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
