"""
技术分析层 - 纯函数算法，无项目依赖，可独立测试
从 data_service.py 拆分出来（2026-08），方便后续加新指标（MACD/布林带等）
数据获取与业务逻辑仍在 data_service.py，本模块只做"算"
"""

import requests
from datetime import datetime, timedelta


# ==============================================================
#  K线获取（腾讯接口）
# ==============================================================

def get_kline(code, days=60, freq="day"):
    """通用K线获取（腾讯接口），支持 day/week/month
    个股/基金专用（market 前缀按 6/5/9 判断沪市）；指数请用 data_service.get_index_history。
    返回 [{"date","open","close","high","low","volume"}, ...]
    """
    market = "sh" if code.startswith(("6", "5", "9")) else "sz"
    end = datetime.now().strftime("%Y-%m-%d")
    # 周线/月线每根跨越多个自然日，起始时间要往前多留
    span = days * 2 if freq == "day" else days * 14
    start = (datetime.now() - timedelta(days=span)).strftime("%Y-%m-%d")
    url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    try:
        resp = requests.get(url, params={"param": f"{market}{code},{freq},{start},{end},{days},qfq"},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = resp.json()
        node = data.get("data", {}).get(f"{market}{code}", {}) or {}
        klines = (node.get(freq, []) or node.get(f"qfq{freq}", []) or
                  node.get("day", []) or node.get("qfqday", []))
        if not klines or len(klines) < 3:
            return []
        return [{"date": k[0], "open": round(float(k[1]), 2), "close": round(float(k[2]), 2),
                 "high": round(float(k[3]), 2), "low": round(float(k[4]), 2),
                 "volume": int(float(k[5])) if len(k) > 5 else 0} for k in klines]
    except Exception:
        return []


# ==============================================================
#  均线 / 斐波那契
# ==============================================================

def calc_ema(prices, period=20):
    """EMA指数移动平均线"""
    if not prices or len(prices) < period: return [None] * len(prices) if prices else []
    multiplier = 2 / (period + 1)
    ema = [None] * (period - 1)
    ema.append(sum(prices[:period]) / period)
    for i in range(period, len(prices)):
        ema.append(round(prices[i] * multiplier + ema[-1] * (1 - multiplier), 2))
    return ema


def calc_fibonacci_levels(high, low, price=None, min_span_pct=0.04):
    """斐波那契回调线（自适应展宽，避免横盘时线条重叠）

    price: 当前价，用于计算最小区间（传 None 则不展宽，保持原行为）
    min_span_pct: 高低区间至少占当前价的比例，实际区间小于该值时
                  自动把高低点向外扩，保证 5 条回调线之间有可读间距
    """
    diff = high - low
    expanded = False
    if price and price > 0:
        min_span = price * min_span_pct
        if diff < min_span:
            pad = (min_span - diff) / 2
            high += pad
            low -= pad
            diff = high - low
            expanded = True
    return {"high": round(high, 2), "low": round(low, 2),
            "expanded": expanded,
            "levels": {str(k): round(high - diff * k, 2) for k in [0.236, 0.382, 0.5, 0.618, 0.786]}}


# ==============================================================
#  摆动点 / 支撑压力 / 趋势线（画线程序化）
# ==============================================================

def zigzag(highs, lows, threshold_pct=0.05):
    """ZigZag 摆动点识别（画线的"骨架"）

    highs/lows: 最高/最低价序列
    threshold_pct: 反转确认阈值。价格从极值反向波动超过该百分比时，
                   才确认前一个极值为摆动点。阈值越大，摆动点越少、越"大级别"。

    返回 [{"index": i, "price": p, "type": "high"|"low", "unconfirmed": bool}, ...]
    尾部未确认的极值 unconfirmed=True（前端可淡化显示）
    """
    n = len(highs)
    if n < 3:
        return []
    direction = 0          # 1=追踪高点, -1=追踪低点
    ext_idx, ext_price = 0, lows[0]
    points = []
    for i in range(1, n):
        if direction == 0:
            # 初始方向：价格先向上/向下突破阈值
            if highs[i] >= lows[0] * (1 + threshold_pct):
                direction, ext_idx, ext_price = 1, i, highs[i]
            elif lows[i] <= lows[0] * (1 - threshold_pct):
                direction, ext_idx, ext_price = -1, i, lows[i]
        elif direction == 1:
            if highs[i] > ext_price:
                ext_idx, ext_price = i, highs[i]      # 新高，继续追踪
            elif lows[i] < ext_price * (1 - threshold_pct):
                points.append({"index": ext_idx, "price": round(ext_price, 2), "type": "high"})
                direction, ext_idx, ext_price = -1, i, lows[i]
        else:  # direction == -1
            if lows[i] < ext_price:
                ext_idx, ext_price = i, lows[i]       # 新低，继续追踪
            elif highs[i] > ext_price * (1 + threshold_pct):
                points.append({"index": ext_idx, "price": round(ext_price, 2), "type": "low"})
                direction, ext_idx, ext_price = 1, i, highs[i]
    if direction != 0:
        points.append({"index": ext_idx, "price": round(ext_price, 2),
                       "type": "high" if direction == 1 else "low", "unconfirmed": True})
    return points


def _cluster_prices(prices, tol_pct):
    """一维贪心聚类：价格差在 tol_pct 以内的归为一组（形成价位带）"""
    groups = []
    for p in sorted(prices):
        if groups and (p - groups[-1][-1]) / groups[-1][-1] <= tol_pct:
            groups[-1].append(p)
        else:
            groups.append([p])
    return groups


def support_resistance(swing_points, tol_pct=0.03):
    """从摆动点聚类出支撑/压力位（水平线）

    摆动高点聚成压力带、摆动低点聚成支撑带，同一带内的点取均价。
    strength = 该带触及次数 / 全部摆动点数（占比），次数越多越强。
    返回 {"support": [{price,touches,strength}...], "resistance": [...]}（按触及次数降序）
    """
    highs = [p["price"] for p in swing_points if p["type"] == "high" and not p.get("unconfirmed")]
    lows = [p["price"] for p in swing_points if p["type"] == "low" and not p.get("unconfirmed")]

    def build(prices, kind):
        result = []
        total = max(len(prices), 1)
        for g in _cluster_prices(prices, tol_pct):
            if not g:
                continue
            result.append({
                "price": round(sum(g) / len(g), 2),
                "touches": len(g),
                "strength": round(len(g) * 100 / total, 1),
                "type": kind,
            })
        result.sort(key=lambda x: -x["touches"])
        return result

    return {"support": build(lows, "support"), "resistance": build(highs, "resistance")}


def find_trendlines(swing_points):
    """从摆动点识别最新趋势线（斜线）

    上升趋势线 = 最后 3 个连续抬高的摆动低点连线
    下降趋势线 = 最后 3 个连续降低的摆动高点连线
    返回 {"up": {"p1": {"x": idx, "y": price}, "p2": {...}} | None,
          "down": {...} | None}
    """
    confirmed = [p for p in swing_points if not p.get("unconfirmed")]
    lows = [p for p in confirmed if p["type"] == "low"]
    highs = [p for p in confirmed if p["type"] == "high"]

    def rising(points, need=3):
        if len(points) < need:
            return None
        tail = points[-need:]
        if all(tail[i]["price"] < tail[i + 1]["price"] for i in range(need - 1)):
            return tail
        return None

    def falling(points, need=3):
        if len(points) < need:
            return None
        tail = points[-need:]
        if all(tail[i]["price"] > tail[i + 1]["price"] for i in range(need - 1)):
            return tail
        return None

    def to_pair(tail):
        return {"p1": {"x": tail[0]["index"], "y": tail[0]["price"]},
                "p2": {"x": tail[-1]["index"], "y": tail[-1]["price"]}}

    up, down = rising(lows), falling(highs)
    return {"up": to_pair(up) if up else None,
            "down": to_pair(down) if down else None}
