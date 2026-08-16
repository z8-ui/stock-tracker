"""
AI 分析模块 - K线数据 + 技术指标 + DeepSeek 大模型分析
职责:
  1. get_klines        拉取日K线(腾讯接口, 前复权)
  2. compute_indicators 计算技术指标(MA/MACD/RSI/量能/斐波那契)
  3. build_prompt      组装结构化 prompt
  4. call_deepseek     调用 DeepSeek 生成分析
  5. analyze           对外主函数: 输入代码 -> 返回分析文本+指标

API key 从 config_local.py 读取(DEEPSEEK_API_KEY), 不提交 GitHub
"""

import requests
from datetime import datetime, timedelta

from config import DEEPSEEK_API_KEY
from data_service import calc_fibonacci_levels

# 腾讯 K线接口(与 routes.py 斐波那契接口同源)
KLINE_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

# DeepSeek 官方接口
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"


# ========== 1. 数据 ==========

def get_klines(code, days=150):
    """拉取日K线, 返回 [{date, open, close, high, low, volume}, ...] 升序"""
    market = "sh" if code.startswith("6") else "sz"
    end = datetime.now().strftime("%Y-%m-%d")
    # 一次多拉一些, 保证 MA60/MACD 有足够预热数据
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            KLINE_URL,
            params={"param": f"{market}{code},day,{start},{end},{days},qfq"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        data = resp.json()
        node = data.get("data", {}).get(f"{market}{code}", {})
        klines = node.get("day") or node.get("qfqday") or []
        if not klines:
            return []
        result = []
        for k in klines:
            result.append({
                "date": k[0],
                "open": float(k[1]),
                "close": float(k[2]),
                "high": float(k[3]),
                "low": float(k[4]),
                "volume": float(k[5]) if len(k) > 5 else 0
            })
        return result
    except Exception:
        return []


# ========== 2. 技术指标 ==========

def _ema(values, period):
    """EMA 序列(从第一个值开始递推)"""
    if not values:
        return []
    k = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _ma(values, period):
    """MA 序列, 前 period-1 个为 None"""
    if len(values) < period:
        return [None] * len(values)
    out = [None] * (period - 1)
    s = sum(values[:period])
    out.append(s / period)
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out.append(s / period)
    return out


def _rsi(closes, period=14):
    """RSI(14): 平均涨幅 / (平均涨幅+平均跌幅) * 100"""
    if len(closes) <= period:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def compute_indicators(klines):
    """计算全部指标, 返回 dict 供 prompt 使用"""
    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    vols = [k["volume"] for k in klines]

    last = klines[-1]
    prev = klines[-2] if len(klines) > 1 else last

    # 涨跌幅
    chg_pct = round((last["close"] - prev["close"]) / prev["close"] * 100, 2)

    # 均线
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)

    # MACD(12,26,9)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    macd_hist = 2 * (dif[-1] - dea[-1])

    # 量能: 近5日均量 / 前20日均量
    vol_ma5 = sum(vols[-5:]) / 5
    vol_ma20 = sum(vols[-20:]) / 20
    vol_ratio = round(vol_ma5 / vol_ma20, 2) if vol_ma20 > 0 else 0

    # 近期高低点
    high_20 = max(highs[-20:])
    low_20 = min(lows[-20:])
    high_60 = max(highs[-60:]) if len(highs) >= 60 else max(highs)
    low_60 = min(lows[-60:]) if len(lows) >= 60 else min(lows)

    # 斐波那契(近20日)
    fib = calc_fibonacci_levels(high_20, low_20)

    # 近20日涨跌分布
    up_days = sum(1 for i in range(-20, 0) if klines[i]["close"] >= klines[i - 1]["close"])
    down_days = 20 - up_days

    return {
        "date": last["date"],
        "price": round(last["close"], 2),
        "chg_pct": chg_pct,
        "ma5": round(ma5[-1], 2) if ma5[-1] else None,
        "ma10": round(ma10[-1], 2) if ma10[-1] else None,
        "ma20": round(ma20[-1], 2) if ma20[-1] else None,
        "ma60": round(ma60[-1], 2) if ma60[-1] else None,
        "macd_dif": round(dif[-1], 3),
        "macd_dea": round(dea[-1], 3),
        "macd_hist": round(macd_hist, 3),
        "rsi14": _rsi(closes),
        "vol_ma5": vol_ma5,
        "vol_ma20": vol_ma20,
        "vol_ratio": vol_ratio,
        "high_20": round(high_20, 2),
        "low_20": round(low_20, 2),
        "high_60": round(high_60, 2),
        "low_60": round(low_60, 2),
        "up_days_20": up_days,
        "down_days_20": down_days,
        "fib": fib,
        # 近40日收盘价序列(给模型看走势轮廓, 采样压缩)
        "trend_snapshot": [round(c, 2) for c in closes[-40:]],
        "vol_snapshot": [round(v / 10000, 1) for v in vols[-40:]]  # 万手
    }


# ========== 3. Prompt ==========

def build_prompt(name, code, ind):
    """组装结构化 prompt, 要求模型输出固定格式的分析"""
    fib_lines = "\n".join(
        f"      {k}: {v}" for k, v in ind["fib"]["levels"].items()
    )
    trend = ", ".join(str(x) for x in ind["trend_snapshot"])
    vols = ", ".join(str(x) for x in ind["vol_snapshot"])

    return f"""你是一名A股技术分析师。请基于以下真实K线数据, 对 {name}({code}) 进行分析。

【最新行情】(日期 {ind['date']})
  收盘价: {ind['price']}  当日涨跌幅: {ind['chg_pct']}%

【均线】
  MA5={ind['ma5']}  MA10={ind['ma10']}  MA20={ind['ma20']}  MA60={ind['ma60']}

【MACD(12,26,9)】
  DIF={ind['macd_dif']}  DEA={ind['macd_dea']}  柱={ind['macd_hist']}

【RSI(14)】 {ind['rsi14']}

【量能】
  5日均量={ind['vol_ma5']:.0f}手  20日均量={ind['vol_ma20']:.0f}手  量比={ind['vol_ratio']}

【区间】
  近20日 高={ind['high_20']} 低={ind['low_20']} (上涨{ind['up_days_20']}天/下跌{ind['down_days_20']}天)
  近60日 高={ind['high_60']} 低={ind['low_60']}

【斐波那契回调位(近20日高低点)】
{fib_lines}

【近40日收盘价序列】
{trend}

【近40日成交量(万手)】
{vols}

请按以下结构输出分析(用纯文本, 每节用 --- 分隔, 关键数字保留):
一、趋势研判: 当前处于什么阶段(上升/下降/震荡/反弹), 依据是什么
二、关键位: 明确的支撑位和阻力位(结合均线/斐波那契/近期高低点)
三、量能与指标: 量价配合情况, MACD/RSI 状态解读
四、风险提示: 最需要注意的风险点
五、一句话结论
注意: 客观分析, 不预测必然涨跌, 不给出买卖指令, 提示风险。"""


# ========== 4. DeepSeek 调用 ==========

def call_deepseek(prompt):
    """调用 DeepSeek chat API, 返回分析文本"""
    if not DEEPSEEK_API_KEY:
        return "未配置 DEEPSEEK_API_KEY: 请创建 config_local.py 并填写自己的 key(在 https://platform.deepseek.com 申请)"
    try:
        resp = requests.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": "你是严谨的A股技术分析师, 输出简洁专业的中文分析。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.5,
                "max_tokens": 1200
            },
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"AI 分析调用失败: {e}"


# ========== 5. 对外主函数 ==========

def analyze(code, name):
    """输入代码+名称, 返回 {analysis, indicators}"""
    klines = get_klines(code)
    if len(klines) < 30:
        return None
    ind = compute_indicators(klines)
    prompt = build_prompt(name, code, ind)
    analysis = call_deepseek(prompt)
    return {"analysis": analysis, "indicators": ind}
