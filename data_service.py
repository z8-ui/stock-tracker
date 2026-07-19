"""
数据层 - 直接请求免费公开 API + 模拟兜底
数据来源：腾讯行情（稳定）+ 东方财富（辅助）+ 模拟数据（兜底）
"""

import requests
import json
import random
from datetime import datetime, timedelta

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/"
}


def _get(url, params=None, timeout=8):
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        resp.encoding = "utf-8"
        return resp.text
    except:
        return None


# ==============================================================
#  腾讯行情接口（最稳定，任何时候都可用）
# ==============================================================

def _tencent_quote(code):
    """获取腾讯个股/指数行情"""
    market = "sh" if code.startswith("6") or code == "000001" else "sz"
    url = f"http://qt.gtimg.cn/q={market}{code}"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        resp.encoding = "gbk"
        parts = resp.text.split("~")
        return parts if len(parts) > 50 else None
    except:
        return None


# ==============================================================
#  1. 全市资金走向（腾讯大盘 + 模拟拆分）
# ==============================================================

def get_market_flow():
    """全市资金流向"""
    parts = _tencent_quote("000001")
    date = datetime.now().strftime("%Y-%m-%d")
    index_name = "上证指数"
    index_price = 0
    index_change = 0

    if parts:
        index_name = parts[1] if parts[1] else "上证指数"
        index_price = float(parts[3]) if parts[3] else 0
        index_change = float(parts[32]) if len(parts) > 32 and parts[32] else 0
        date = parts[30][:8] if len(parts) > 30 and parts[30] else date

    # 根据大盘涨跌模拟资金流向
    sign = 1 if index_change > 0 else -1
    base = abs(index_change) * random.uniform(1.5, 3.0)
    
    return {
        "main_force": round(sign * base, 2),
        "super_large": round(sign * base * 0.45, 2),
        "large_order": round(sign * base * 0.25, 2),
        "middle_order": round(-sign * base * 0.2, 2),
        "small_order": round(-sign * base * 0.5, 2),
        "date": date,
        "index_name": index_name,
        "index_price": index_price,
        "index_change": index_change
    }


# ==============================================================
#  2. 板块热力图（固定板块 + 模拟涨跌）
# ==============================================================

# 固定板块列表（A股行业板块）
SECTOR_LIST = [
    "银行", "保险", "证券", "房地产开发", "半导体",
    "软件开发", "通信设备", "汽车整车", "汽车零部件", "锂电池",
    "白酒", "食品饮料", "医药商业", "化学制药", "医疗器械",
    "煤炭开采", "钢铁", "有色金属", "电力", "光伏设备",
    "军工装备", "家电", "纺织服装", "商业百货", "文化传媒",
    "游戏", "航空机场", "物流", "工程建设", "水泥建材"
]

def get_sector_flow():
    """行业板块数据"""
    result = []
    
    # 优先用东方财富真实数据
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "60", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:90+t:2",
        "fields": "f14,f2,f3,f62"
    }
    text = _get(url, params)
    
    if text:
        try:
            data = json.loads(text)
            if data.get("data") and data["data"].get("diff"):
                for row in data["data"]["diff"]:
                    name = row.get("f14", "")
                    change = row.get("f3", 0)
                    flow = row.get("f62", 0)
                    if name and change is not None:
                        result.append({
                            "name": name,
                            "change": round(float(change), 2),
                            "flow": round(float(flow) / 1e8, 2)
                        })
        except:
            pass
    
    # 兜底：基于真实大盘涨跌幅的模拟（不是完全随机）
    if not result:
        # 先拿真实大盘指数
        market_change = _get_market_change()
        random.seed(datetime.now().strftime("%Y%m%d"))
        for name in SECTOR_LIST:
            # 板块涨跌幅围绕大盘涨跌幅波动，幅度±2%
            # 如大盘跌-3%，板块在 -5% ~ -1% 之间，少数防守板块可能微涨
            offset = random.uniform(-2.0, 2.0)
            change = round(market_change + offset, 2)
            # 防守板块（银行、电力、食品饮料等）相对抗跌
            defensive = ["银行", "电力", "食品饮料", "煤炭开采", "有色金属", "医药商业", "化学制药"]
            if name in defensive and market_change < 0:
                change = round(change + random.uniform(1.0, 3.0), 2)  # 抗跌溢价
                change = min(change, 1.5)  # 最多微涨，不会大涨
            # 资金流与涨跌幅正相关
            flow = round(change * random.uniform(2, 6), 2)
            result.append({"name": name, "change": change, "flow": flow})
        result.sort(key=lambda x: x["change"], reverse=True)
    
    return result if result else None


def _get_market_change():
    """获取真实大盘涨跌幅作为基准"""
    try:
        resp = requests.get("http://qt.gtimg.cn/q=sh000001",
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        resp.encoding = "gbk"
        parts = resp.text.split("~")
        if len(parts) > 32 and parts[32]:
            return float(parts[32])
    except:
        pass
    return -0.5  # 默认微跌


# ==============================================================
#  3. 个股行情（腾讯，最稳定）
# ==============================================================

def get_stock_quote(stock_code):
    """个股实时行情"""
    parts = _tencent_quote(stock_code)
    if not parts:
        return None
    
    try:
        name = parts[1]
        price = float(parts[3]) if parts[3] else 0
        pre_close = float(parts[4]) if parts[4] else 0
        open_price = float(parts[5]) if parts[5] else 0
        high = float(parts[33]) if len(parts) > 33 and parts[33] else 0
        low = float(parts[34]) if len(parts) > 34 and parts[34] else 0
        change = float(parts[31]) if parts[31] else 0
        change_pct = float(parts[32]) if parts[32] else 0
        volume_lots = int(parts[6]) if parts[6] else 0
        pe = float(parts[39]) if len(parts) > 39 and parts[39] else 0
        pb = float(parts[46]) if len(parts) > 46 and parts[46] else 0
        turnover_rate = float(parts[38]) if len(parts) > 38 and parts[38] else 0
        market_cap = float(parts[44]) if len(parts) > 44 and parts[44] else 0
        turnover = float(parts[57]) if len(parts) > 57 and parts[57] else 0
        amplitude = float(parts[43]) if len(parts) > 43 and parts[43] else 0
        high_52w = float(parts[47]) if len(parts) > 47 and parts[47] else 0
        low_52w = float(parts[48]) if len(parts) > 48 and parts[48] else 0

        return {
            "name": name, "code": stock_code,
            "price": price, "pre_close": pre_close, "open": open_price,
            "high": high, "low": low,
            "change": change, "change_pct": change_pct,
            "volume_lots": volume_lots,
            "turnover": round(turnover / 10000, 2),
            "turnover_rate": turnover_rate, "amplitude": amplitude,
            "pe": pe, "pb": pb, "market_cap": market_cap,
            "high_52w": high_52w, "low_52w": low_52w
        }
    except:
        return None


# ==============================================================
#  4. 个股资金流向（基于真实成交额的模拟趋势）
# ==============================================================

def get_stock_money_flow(stock_code):
    """个股资金流向趋势"""
    quote = get_stock_quote(stock_code)
    if not quote:
        return None

    turnover = max(quote.get("turnover", 1), 0.1)
    random.seed(int(stock_code) + int(datetime.now().timestamp() / 86400))

    dates = []
    super_large = []
    large = []
    middle = []
    small = []

    today = datetime.now()
    for i in range(20):
        day = today - timedelta(days=(19 - i))
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        dates.append(day.strftime("%m-%d"))

        t = turnover / 20 * random.uniform(0.7, 1.3)
        sl = round(t * random.uniform(0.15, 0.35), 2)
        lg = round(t * random.uniform(0.1, 0.25), 2)
        md = round(-t * random.uniform(0.1, 0.2), 2)
        sm = round(-t * random.uniform(0.15, 0.3), 2)

        # 趋势一致性：相邻日期不要突变
        if super_large and abs(sl - super_large[-1]) > abs(super_large[-1]) * 0.5:
            sl = round(super_large[-1] * random.uniform(0.7, 1.3), 2)

        super_large.append(sl)
        large.append(lg)
        middle.append(md)
        small.append(sm)

    return {
        "dates": dates,
        "flows": {
            "super_large": super_large,
            "large": large,
            "middle": middle,
            "small": small
        }
    }


# ==============================================================
#  5. 估值数据（腾讯真实PE + 行业对比）
# ==============================================================

# 行业平均PE对照表
INDUSTRY_PE_MAP = {
    "600519": 35, "000858": 30,  # 白酒
    "300750": 40, "002594": 45,  # 新能源
    "600036": 7, "601398": 6, "601166": 5,  # 银行
    "601318": 12, "601628": 15,  # 保险
    "000333": 15, "000651": 12,  # 家电
    "600276": 40, "300760": 45,  # 医药
    "600900": 22,  # 电力
    "300059": 35,  # 券商/金融
    "600887": 25, "600882": 20,  # 食品
    "002415": 25, "000063": 30,  # 通信
}

def get_stock_valuation(stock_code):
    """个股估值"""
    quote = get_stock_quote(stock_code)
    if not quote:
        return None

    pe = quote.get("pe", 0)
    pb = quote.get("pb", 0)
    industry_pe = INDUSTRY_PE_MAP.get(stock_code, 25)
    
    if pe == 0:
        pe = industry_pe

    deviation_pe = round((pe - industry_pe) / industry_pe * 100, 1)

    return {
        "pe": round(pe, 2),
        "pb": round(pb, 2),
        "pe_ttm": round(pe, 2),
        "industry_pe": industry_pe,
        "deviation_pe": deviation_pe,
        "level": "偏高" if deviation_pe > 20 else ("偏低" if deviation_pe < -20 else "合理"),
        "date": datetime.now().strftime("%Y-%m-%d")
    }


# ==============================================================
#  6. 搜索股票（东方财富）
# ==============================================================

def search_stock(keyword):
    """搜索股票"""
    url = "https://searchadapter.eastmoney.com/api/suggest/get"
    params = {
        "input": keyword, "type": 14,
        "token": "D43BF722C8E33BDC906FB84D85E326E8",
        "count": 10
    }
    text = _get(url, params)
    if not text:
        return None
    try:
        data = json.loads(text)
        result = []
        if data.get("QuotationCodeTable") and data["QuotationCodeTable"].get("Data"):
            for item in data["QuotationCodeTable"]["Data"]:
                code = item.get("Code", "")
                name = item.get("Name", "")
                if code and name:
                    result.append({"code": code, "name": name})
        return result if result else None
    except:
        return None


# ==============================================================
#  7. 指数历史K线（腾讯 - 用于EMA/斐波那契计算）
# ==============================================================

def get_index_history(index_code, days=30):
    """获取指数历史K线"""
    market = "sh" if index_code.startswith("0") or index_code.startswith("6") else "sz"
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    
    url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    try:
        resp = requests.get(url, params={"param": f"{market}{index_code},day,{start},{end},{days},qfq"},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = resp.json()
        if not data.get("data"): return _mock_index_history(index_code, days)
        
        klines = (data["data"].get(f"{market}{index_code}", {}).get("day", []) or
                  data["data"].get(f"{market}{index_code}", {}).get("qfqday", []))
        if not klines or len(klines) < 5: return _mock_index_history(index_code, days)
        
        return [{"date": k[0], "open": round(float(k[1]), 2), "close": round(float(k[2]), 2),
                 "high": round(float(k[3]), 2), "low": round(float(k[4]), 2),
                 "volume": int(k[5]) if len(k) > 5 else 0} for k in klines]
    except:
        return _mock_index_history(index_code, days)


def _mock_index_history(index_code, days=30):
    """模拟历史K线兜底"""
    market = "sh" if index_code.startswith("0") or index_code.startswith("6") else "sz"
    try:
        r = requests.get(f"http://qt.gtimg.cn/q={market}{index_code}",
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        r.encoding = "gbk"
        parts = r.text.split("~")
        cur_price = float(parts[3]) if len(parts) > 3 and parts[3] else 2000
    except:
        cur_price = 2000
    
    random.seed(index_code)
    result = []
    today = datetime.now()
    price = cur_price * 0.85
    
    for i in range(days):
        day = today - timedelta(days=(days - 1 - i))
        while day.weekday() >= 5: day -= timedelta(days=1)
        change = random.uniform(-0.03, 0.03)
        price *= (1 + change)
        result.append({"date": day.strftime("%Y-%m-%d"),
                      "open": round(price * (1 - change * 0.3), 2),
                      "close": round(price, 2),
                      "high": round(price * (1 + abs(random.uniform(0, 0.02))), 2),
                      "low": round(price * (1 - abs(random.uniform(0, 0.02))), 2),
                      "volume": random.randint(5000000, 30000000)})
    return result


# ==============================================================
#  8. 技术分析
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


def calc_fibonacci_levels(high, low):
    """斐波那契回调线"""
    diff = high - low
    return {"high": round(high, 2), "low": round(low, 2),
            "levels": {str(k): round(high - diff * k, 2) for k in [0.236, 0.382, 0.5, 0.618, 0.786]}}


def estimate_intrinsic_value(stock_quote):
    """估算内在价值"""
    pe = stock_quote.get("pe", 25) or 25
    price = stock_quote.get("price", 0)
    code = stock_quote.get("code", "")
    industry_pe = INDUSTRY_PE_MAP.get(code, 25)
    eps = price / pe if pe and price else 0
    intrinsic = round(eps * industry_pe * 0.8, 2) if eps else price
    return {"price": price, "intrinsic_value": intrinsic,
            "gap": round((price - intrinsic) / intrinsic * 100, 1) if intrinsic else 0,
            "pe": pe, "industry_pe": industry_pe, "eps": round(eps, 3) if eps else 0}


# ==============================================================
#  9. 板块估值
# ==============================================================

def get_sector_valuation():
    """各行业板块估值（PE/PB）"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {"pn": "1", "pz": "60", "po": "1", "np": "1",
              "ut": "bd1d9ddb04089700cf9c27f6f7426281",
              "fltt": "2", "invt": "2", "fid": "f3",
              "fs": "m:90+t:2", "fields": "f14,f2,f3,f9,f20,f23,f25,f37"}
    text = _get(url, params)
    if not text: return _mock_sector_valuation()
    try:
        data = json.loads(text)
        result = []
        if data.get("data") and data["data"].get("diff"):
            for row in data["data"]["diff"]:
                name = row.get("f14", ""); pe = row.get("f9", 0)
                pb = row.get("f23", 0); change = row.get("f3", 0)
                mc = row.get("f20", 0)
                if name and pe:
                    result.append({"name": name, "pe": round(float(pe), 2) if pe else 0,
                                   "pb": round(float(pb), 2) if pb else 0,
                                   "change": round(float(change), 2) if change else 0,
                                   "market_cap": round(float(mc) / 1e8, 2) if mc else 0})
        return result if result else _mock_sector_valuation()
    except:
        return _mock_sector_valuation()


def _mock_sector_valuation():
    """模拟板块估值"""
    sectors = [("白酒",35,8),("银行",6,0.7),("保险",12,1.5),("证券",22,1.8),
               ("半导体",60,5),("软件开发",55,4.5),("医药生物",40,4),("新能源",42,3.5),
               ("光伏",35,3),("汽车整车",28,2.5),("家电",15,2.8),("食品饮料",30,6),
               ("煤炭",8,1.2),("有色金属",18,2.5),("电力",22,1.8),("军工",55,3.5),
               ("通信",32,2.8),("房地产",10,0.8),("化工",20,2.2),("机械设备",25,2.5)]
    random.seed(datetime.now().strftime("%Y%m"))
    return [{"name": n, "pe": round(p * random.uniform(0.7,1.3), 2),
             "pb": round(q * random.uniform(0.8,1.2), 2),
             "change": round(random.uniform(-3,3), 2),
             "market_cap": round(random.uniform(500,30000), 2)} for n, p, q in sectors]
