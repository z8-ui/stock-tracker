"""
数据层 - 直接请求免费公开 API + 模拟兜底
数据来源：腾讯行情（稳定）+ 东方财富（辅助）+ 模拟数据（兜底）
"""

import requests
import json
import random
import os
import time
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from policy_notes import get_policy_notes

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/"
}

# ==============================================================
#  文件持久化缓存（重启不丢数据，休盘时段显示定格快照）
# ==============================================================

_CACHE_FILE = os.path.join(os.path.dirname(__file__), "_data_cache.json")

def _load_cache():
    """从文件加载缓存"""
    if not os.path.exists(_CACHE_FILE):
        return {}
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

_CACHE_LOCK = threading.Lock()  # 文件缓存写锁（原子替换，防多线程并发写坏）

def _save_cache(cache_dict):
    """保存缓存到文件（临时文件 + 原子替换，后台 SWR 线程并发安全）"""
    try:
        with _CACHE_LOCK:
            tmp = _CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache_dict, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _CACHE_FILE)
    except:
        pass


# ==============================================================
#  Short-lived in-memory cache（交易时段 8 秒 TTL，减少重复外部调用）
#  2026-08 升级：SWR(过期先返回旧数据后台重建) + single-flight(并发只建一次)
# ==============================================================

_IN_MEMORY_CACHE = {}   # key -> {"data": ..., "time": fetched_at}
_IN_MEMORY_TTL = 8      # seconds
_IN_FLIGHT = {}         # key -> threading.Event，正在构建中的请求（single-flight）
_LAST_SUCCESS = {}      # key -> float，最后成功构建时间（/api/ready 硬过期判断用）


def _get_from_cache(key):
    """交易时段内存缓存：TTL 内返回已有结果，不调外部 API"""
    cached = _IN_MEMORY_CACHE.get(key)
    if cached and (time.time() - cached['time']) < _IN_MEMORY_TTL:
        return cached['data']
    return None


def _set_in_cache(key, data):
    _IN_MEMORY_CACHE[key] = {'data': data, 'time': time.time()}


def _spawn_refresh(cache_key, fetcher, validate_fn):
    """SWR 后台刷新：不阻塞当前请求，重建成功后更新内存+文件缓存
    single-flight：同 key 已在刷新则直接跳过
    """
    def _refresh():
        if cache_key in _IN_FLIGHT:
            return
        evt = threading.Event()
        _IN_FLIGHT[cache_key] = evt
        try:
            fresh = fetcher()
            if validate_fn is None or validate_fn(fresh):
                ts = time.time()
                _IN_MEMORY_CACHE[cache_key] = {"data": fresh, "time": ts}
                _LAST_SUCCESS[cache_key] = ts
                cache = _load_cache()
                cache[cache_key] = fresh
                _save_cache(cache)
        except Exception:
            pass
        finally:
            _IN_FLIGHT.pop(cache_key, None)
            evt.set()

    threading.Thread(target=_refresh, daemon=True).start()


def _get(url, params=None, timeout=8):
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        resp.encoding = "utf-8"
        return resp.text
    except:
        return None


# ==============================================================
#  市场交易时段检测 + 文件持久化缓存
# ==============================================================

def _market_status():
    """判断当前 A 股交易状态
    返回: ('交易中'|'午间休盘'|'已收盘'|'周末休市', is_trading)
    """
    now = datetime.now()
    if now.weekday() >= 5:
        return ("周末休市", False)
    h, m = now.hour, now.minute
    minutes = h * 60 + m
    if 9*60+15 <= minutes < 9*60+30:
        return ("集合竞价", True)
    if 9*60+30 <= minutes < 11*60+30:
        return ("交易中", True)
    if 11*60+30 <= minutes < 13*60:
        return ("午间休盘", False)
    if 13*60 <= minutes < 15*60:
        return ("交易中", True)
    return ("已收盘", False)


def _has_valid_data(data):
    """检查 dict 数据是否有真实值（非全零/全空）"""
    if not data:
        return False
    # 对于市场资金流：必须有真实的 fund flow 数据（主力资金非零或拆分项非零）
    if "main_force" in data:
        # 检查任意一项 fund flow 是否为真实值
        flow_fields = ["main_force", "super_large", "large_order", "middle_order", "small_order"]
        has_flow = any(abs(data.get(f, 0)) > 0.01 for f in flow_fields)
        has_index = abs(data.get("index_price", 0)) > 0.01
        # 必须有 index 数据（腾讯稳定返回）且最好有 flow 数据
        # 若完全没有 flow 但 index 正常 → 缓存不可用（非交易时段抓到的空数据）
        return has_index and has_flow
    return True


def _cached_get(cache_key, fetcher, validate_fn=None, source="", ttl=None, force=False):
    """统一的缓存读写（SWR + single-flight + 数据标注注入）

    cache_key:    字符串标识
    fetcher:      无参函数，返回可 JSON 序列化的数据
    validate_fn:  可选，接收数据返回 bool，判断数据是否有效
    source:       数据源名称（如 "tencent"/"eastmoney"），注入返回数据
    ttl:          内存缓存秒数（默认 _IN_MEMORY_TTL）
    force:        为 True 时跳过全部缓存，强制同步拉取并写回缓存（手动更新按钮用）

    行为：
    - 交易时段：TTL 内命中内存直接返回；过期先返回旧数据并后台重建（SWR）；
      并发同 key 只允许一次真实构建（single-flight）；构建失败降级文件缓存
    - 非交易时段：优先文件缓存（定格快照），无缓存才抓一次
    - force=True：无视交易时段/缓存，直接抓取并写回（抓取失败仍降级文件缓存）
    - 返回的 dict 自动注入 _asof(数据获取时间)/_market_status/_source
    """
    status, is_trading = _market_status()
    ttl = ttl or _IN_MEMORY_TTL
    now = time.time()
    cache = _load_cache()

    def _inject(value, asof):
        if isinstance(value, dict):
            value["_asof"] = asof
            value["_market_status"] = status
            if source:
                value["_source"] = source
        return value

    def _store(value):
        _IN_MEMORY_CACHE[cache_key] = {"data": value, "time": now}
        _LAST_SUCCESS[cache_key] = now
        cache[cache_key] = value
        _save_cache(cache)
        return value

    def _file_cache():
        val = cache.get(cache_key)
        if val is not None:
            return _inject(val, _LAST_SUCCESS.get(cache_key, now))
        return None

    # 强制刷新：跳过所有缓存层，同步拉取并写回
    if force:
        fresh = fetcher()
        if validate_fn is None or validate_fn(fresh):
            return _inject(_store(fresh), now)
        cached = _file_cache()
        if cached is not None:
            return cached
        return fresh

    if is_trading:
        mem = _IN_MEMORY_CACHE.get(cache_key)
        # 1) 新鲜内存 → 直接返回
        if mem and (now - mem["time"]) < ttl:
            return _inject(mem["data"], mem["time"])
        # 2) 过期内存 → SWR：先返回旧数据，后台异步重建
        if mem:
            _spawn_refresh(cache_key, fetcher, validate_fn)
            return _inject(mem["data"], mem["time"])
        # 3) 无内存 → single-flight 同步构建（并发只建一次，检查+注册原子化）
        with _CACHE_LOCK:
            evt = _IN_FLIGHT.get(cache_key)
            if evt:
                waiting = True
            else:
                evt = threading.Event()
                _IN_FLIGHT[cache_key] = evt
                waiting = False
        if waiting:
            evt.wait(5)  # 别人正在构建：最多等 5 秒
            mem = _IN_MEMORY_CACHE.get(cache_key)
            if mem:
                return _inject(mem["data"], mem["time"])
            cached = _file_cache()
            if cached is not None:
                return cached
            return None
        try:
            fresh = fetcher()
            if validate_fn is None or validate_fn(fresh):
                return _inject(_store(fresh), now)
            # 构建失败 → 文件缓存兜底
            cached = _file_cache()
            if cached is not None:
                return cached
            return fresh
        finally:
            with _CACHE_LOCK:
                _IN_FLIGHT.pop(cache_key, None)
            evt.set()

    # 非交易时段：优先文件缓存（定格快照）
    cached = _file_cache()
    if cached is not None:
        return cached
    # 无缓存（全新部署/首次启动在休盘）：试着抓一次
    fresh = fetcher()
    if validate_fn is None or validate_fn(fresh):
        return _inject(_store(fresh), now)
    return fresh


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
    """全市资金流向（基于东方财富真实数据 + 文件缓存）"""
    def _fetch():
        date = datetime.now().strftime("%Y-%m-%d")
        index_name = "上证指数"
        index_price = 0
        index_change = 0

        # 并行调用腾讯（大盘行情） + 东方财富（资金流向）
        parts = None
        text = None
        eastmoney_url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        eastmoney_params = {
            "fltt": "2",
            "fields": "f62,f66,f69,f72,f75,f78,f81,f84,f87",
            "secids": "1.000001,0.399001"
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            tencent_fut = pool.submit(_tencent_quote, "000001")
            em_fut = pool.submit(_get, eastmoney_url, eastmoney_params)
            parts = tencent_fut.result()
            text = em_fut.result()

        if parts:
            index_name = parts[1] if parts[1] else "上证指数"
            index_price = float(parts[3]) if parts[3] else 0
            index_change = float(parts[32]) if len(parts) > 32 and parts[32] else 0
            date = parts[30][:8] if len(parts) > 30 and parts[30] else date
        main_force = 0
        super_large = 0
        large_order = 0
        middle_order = 0
        small_order = 0

        if text:
            try:
                data = json.loads(text)
                diff = data.get("data", {}).get("diff", [])
                for item in diff:
                    def val2yi(v):
                        if v is None: return 0
                        v = float(v)
                        return round(v / 1e8, 2) if abs(v) > 1e6 else round(v, 2)
                    main_force += val2yi(item.get("f62", 0))
                    super_large += val2yi(item.get("f66", 0))
                    large_order += val2yi(item.get("f72", 0))
                    middle_order += val2yi(item.get("f69", 0))
                    small_order += val2yi(item.get("f75", 0))
            except:
                pass

        return {
            "main_force": main_force,
            "super_large": super_large,
            "large_order": large_order,
            "middle_order": middle_order,
            "small_order": small_order,
            "date": date,
            "index_name": index_name,
            "index_price": index_price,
            "index_change": index_change
        }

    return _cached_get("market_flow", _fetch, validate_fn=_has_valid_data)


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
    """行业板块数据（带文件缓存，休盘时段返回定格数据）"""
    def _fetch():
        result = []
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
        # 兜底模拟：使用真实大盘涨跌幅 + 板块特性分布
        if not result:
            market_change = _get_market_change()
            random.seed(datetime.now().strftime("%Y%m%d"))
            # 板块分类：成长/周期/防御 各有不同弹性
            growth = ["半导体", "软件开发", "通信设备", "光伏设备", "锂电池", "军工装备", "游戏"]
            cyclical = ["证券", "有色金属", "煤炭开采", "钢铁", "水泥建材", "房地产开发", "汽车整车"]
            defensive = ["银行", "电力", "食品饮料", "煤炭开采", "医药商业", "白酒", "保险"]
            for name in SECTOR_LIST:
                if name in growth:
                    # 成长板块弹性更大：大盘涨+2%~4%，大盘跌-2%~0%
                    offset = random.uniform(-2.0, 4.0)
                elif name in cyclical:
                    offset = random.uniform(-3.0, 3.0)
                else:
                    offset = random.uniform(-1.5, 2.0)
                change = round(market_change + offset, 2)
                # 防守板块在大盘跌时抗跌
                if name in defensive and market_change < -1:
                    change = round(change + random.uniform(1.0, 3.0), 2)
                    change = min(change, 2.0)
                flow = round(change * random.uniform(2, 6), 2)
                result.append({"name": name, "change": change, "flow": flow})
            result.sort(key=lambda x: x["change"], reverse=True)
        return result if result else None

    def _validate_sectors(data):
        if not data or not isinstance(data, list):
            return False
        # 至少有一条数据有非零的 change 或 flow
        for s in data:
            if abs(s.get("change", 0)) > 0.01 or abs(s.get("flow", 0)) > 0.01:
                return True
        return False

    return _cached_get("sector_flow", _fetch, validate_fn=_validate_sectors)


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
#  4b. 个股当日资金净流向 + 占比（东方财富真实数据）
# ==============================================================

def get_stock_real_money_flow(stock_code):
    """个股当日资金净流向及占比（东方财富真实数据）
    返回：{main_force, super_large, large, middle, small} 均为亿元
          {main_force_pct, super_large_pct, large_pct, middle_pct, small_pct} 均为 %
    其中 large_pct 即 大单净量（大单净流入占成交额比例）
    """
    market = "1" if stock_code.startswith("6") else "0"
    secid = f"{market}.{stock_code}"

    # 1) 拿个股成交额（腾讯）
    quote = get_stock_quote(stock_code)
    if not quote:
        return None
    turnover_yi = max(quote.get("turnover", 0.1), 0.01)  # 亿元

    # 2) 拿资金流向明细（东方财富 fflow API）
    url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55",
        "lmt": "1",
        "klt": "101"
    }
    text = _get(url, params)
    if not text:
        return None
    try:
        data = json.loads(text)
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return None
        parts = klines[0].split(",")
        # parts: date, 主力, 小单, 中单, 大单  (单位: 元)
        main_force = float(parts[1]) if len(parts) > 1 else 0
        small_order = float(parts[2]) if len(parts) > 2 else 0
        middle_order = float(parts[3]) if len(parts) > 3 else 0
        large_order = float(parts[4]) if len(parts) > 4 else 0
        # 超大单 = 主力 - 大单
        super_large = main_force - large_order

        # 元 → 亿元
        def yi(v):
            return round(v / 1e8, 2)

        # 计算占比% = 净流入额 / 成交额 * 100
        def pct(v):
            return round(v / (turnover_yi * 1e8) * 100, 2) if turnover_yi > 0 else 0

        return {
            "main_force": yi(main_force),
            "super_large": yi(super_large),
            "large": yi(large_order),
            "middle": yi(middle_order),
            "small": yi(small_order),
            "main_force_pct": pct(main_force),
            "super_large_pct": pct(super_large),
            "large_pct": pct(large_order),       # 大单净量
            "middle_pct": pct(middle_order),
            "small_pct": pct(small_order),
        }
    except:
        return None


# ==============================================================
#  5. 估值数据（腾讯真实PE + 行业对比）
# ==============================================================

# 行业平均PE对照表（仅作 get_industry_pe 失败时的降级兜底）
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


def get_latest_financials(stock_code, refresh=False):
    """最新一期财报（东方财富 F10 主要财务指标）

    返回 {report_date, report_name, eps, bps, revenue, revenue_yoy,
          net_profit, profit_yoy, roe, notice_date} 或 None（失败降级）
    2026-08 新增：估值偏离需结合最新季报（净利润/营收同比、EPS）综合判断
    """
    def fetch():
        if stock_code.startswith(("4", "8", "92")):
            suffix = ".BJ"
        elif stock_code.startswith(("6", "9")):
            suffix = ".SH"
        else:
            suffix = ".SZ"
        try:
            r = requests.get(
                "https://datacenter-web.eastmoney.com/api/data/v1/get",
                params={
                    "reportName": "RPT_F10_FINANCE_MAINFINADATA",
                    "columns": "ALL",
                    "filter": f'(SECUCODE="{stock_code}{suffix}")',
                    "pageNumber": "1", "pageSize": "1",
                    "sortColumns": "REPORT_DATE", "sortTypes": "-1",
                },
                headers=HEADERS, timeout=8)
            rows = ((r.json().get("result") or {}).get("data")) or []
            if not rows:
                return None
            d = rows[0]
            return {
                "report_date": (d.get("REPORT_DATE") or "")[:10],
                "report_name": d.get("REPORT_DATE_NAME") or "",
                "eps": d.get("EPSJB"),                 # 基本每股收益
                "bps": d.get("BPS"),                   # 每股净资产
                "revenue": d.get("TOTALOPERATEREVE"),  # 营业总收入(元)
                "revenue_yoy": d.get("TOTALOPERATEREVETZ"),   # 营收同比%
                "net_profit": d.get("PARENTNETPROFIT"),       # 归母净利润(元)
                "profit_yoy": d.get("PARENTNETPROFITTZ"),     # 净利润同比%
                "roe": d.get("ROEJQ"),                 # 净资产收益率%
                "notice_date": (d.get("NOTICE_DATE") or "")[:10],
            }
        except Exception:
            return None

    return _cached_get(f"fin_{stock_code}", fetch,
                       validate_fn=lambda x: x is not None,
                       source="eastmoney", ttl=6 * 3600, force=refresh)


def get_industry_pe(stock_code, refresh=False):
    """动态获取行业平均 PE（东财三步链：个股→行业名 f127→suggest搜BK→板块PE f9×100）

    失败自动降级：INDUSTRY_PE_MAP 硬编码 → 默认 25（返回 source=static_map 标记）
    返回 {"pe": float, "industry": 行业名, "source": "eastmoney"|"static_map", ...}
    """
    def fetch():
        market = "1" if stock_code.startswith(("6", "9")) else "0"
        try:
            # 1) 个股 → 行业名（f127，如"白酒Ⅱ"）
            r = requests.get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={"secid": f"{market}.{stock_code}", "fields": "f57,f127"},
                headers=HEADERS, timeout=6)
            d = r.json().get("data") or {}
            ind_name = d.get("f127")
            if not ind_name:
                return None
            # 2) 行业名 → 板块代码（suggest type=14 板块，取第一个 BK 开头）
            r2 = requests.get(
                "https://searchapi.eastmoney.com/api/suggest/get",
                params={"input": ind_name, "type": "14", "count": "5"},
                headers=HEADERS, timeout=6)
            data = (r2.json().get("QuotationCodeTable") or {}).get("Data") or []
            bk = next((it.get("Code") for it in data if str(it.get("Code") or "").startswith("BK")), None)
            if not bk:
                return None
            # 3) 板块 → 动态 PE（f9，放大100倍）
            r3 = requests.get(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                params={"secids": f"90.{bk}", "fields": "f12,f14,f9"},
                headers=HEADERS, timeout=6)
            diff = (r3.json().get("data") or {}).get("diff") or []
            if not diff:
                return None
            pe = diff[0].get("f9")
            if not pe or pe <= 0:
                return None
            return {"pe": round(pe / 100, 2),
                    "industry": ind_name,
                    "source": "eastmoney"}
        except Exception:
            return None

    result = _cached_get(f"industry_pe_{stock_code}", fetch,
                         validate_fn=lambda x: x is not None and x.get("pe"),
                         source="eastmoney", ttl=3600, force=refresh)
    if result and result.get("pe"):
        return result
    # 降级：静态映射表（source 标记，前端可提示"估值为静态参考"）
    return {"pe": INDUSTRY_PE_MAP.get(stock_code, 25),
            "industry": "未知", "source": "static_map"}


def get_stock_valuation(stock_code, refresh=False):
    """个股估值（refresh=True 时强制穿透行业PE/财报缓存重新拉取）"""
    quote = get_stock_quote(stock_code)
    if not quote:
        return None

    pe = quote.get("pe", 0)
    pb = quote.get("pb", 0)
    ind = get_industry_pe(stock_code, refresh=refresh)
    industry_pe = ind["pe"]

    if pe == 0:
        pe = industry_pe

    deviation_pe = round((pe - industry_pe) / industry_pe * 100, 1)

    result = {
        "pe": round(pe, 2),
        "pb": round(pb, 2),
        "pe_ttm": round(pe, 2),
        "industry_pe": industry_pe,
        "industry_name": ind.get("industry", "未知"),
        "industry_pe_source": ind.get("source", "unknown"),
        "deviation_pe": deviation_pe,
        "level": "偏高" if deviation_pe > 20 else ("偏低" if deviation_pe < -20 else "合理"),
        "date": datetime.now().strftime("%Y-%m-%d"),
    }

    # 2026-08 新增：估值偏离结合最新季报财报 + 产业政策事件
    # 财报（净利润/营收同比、EPS）动态拉取；政策事件手动维护在 policy_notes.py
    result["financials"] = get_latest_financials(stock_code, refresh=refresh)
    result["policy_notes"] = get_policy_notes(stock_code, result["industry_name"])
    return result


# ==============================================================
#  6. 搜索股票（东方财富）
# ==============================================================

def search_stock(keyword):
    """搜索股票（全市场: A股/港股/美股/指数/基金）
    返回 [{code, name, market, tag}, ...], A股优先排序
    market: sh/sz/bj/hk/us/idx/fund, 用于后续 K线/行情接口的前缀
    """
    url = "https://searchadapter.eastmoney.com/api/suggest/get"
    params = {
        "input": keyword, "type": 14,
        "token": "D43BF7DAA5C9D33F9AB8D13D8F05E26E8",
        "count": 15
    }
    text = _get(url, params)
    if not text:
        return None
    try:
        data = json.loads(text)
        result = []
        seen = set()
        if data.get("QuotationCodeTable") and data["QuotationCodeTable"].get("Data"):
            for item in data["QuotationCodeTable"]["Data"]:
                code = item.get("Code", "")
                name = item.get("Name", "")
                classify = item.get("Classify", "")
                mkt_num = item.get("MktNum")
                if not code or not name:
                    continue
                market, tag = _classify_market(classify, code, mkt_num)
                if market is None or code in seen:
                    continue
                seen.add(code)
                result.append({"code": code, "name": name, "market": market, "tag": tag})
        if not result:
            return None
        # A股优先, 其余按 港股 > 指数 > 基金 > 美股 排序
        order = {"sh": 0, "sz": 0, "bj": 0, "hk": 1, "idx": 2, "fund": 3, "us": 4}
        result.sort(key=lambda x: order.get(x["market"], 9))
        return result[:12]
    except:
        return None


def _classify_market(classify, code, mkt_num):
    """根据东方财富 suggest 接口的 Classify/MktNum 判断市场
    返回 (market前缀, 显示标签); 不支持的品种返回 (None, None)
    """
    if classify == "AStock":
        if code.startswith(("4", "8", "92")):
            return "bj", "北交所"
        if code.startswith("6"):
            return "sh", "A股"
        return "sz", "A股"
    if classify == "HK":
        return "hk", "港股"
    if classify == "Index":
        if str(mkt_num) == "0":
            return "sz", "指数"
        return "sh", "指数"
    if classify == "Fund":
        if code.startswith(("5", "6")):
            return "sh", "基金"
        return "sz", "基金"
    if classify == "UsStock":
        return "us", "美股"
    # 北证50等特殊指数
    if code.startswith("899"):
        return "bj", "指数"
    # 其余(BK板块/KRX韩国/OTCFUND场外基金/UniversalIndex全球指数等)不支持
    return None, None


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
                 "volume": int(float(k[5])) if len(k) > 5 else 0} for k in klines]
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



def estimate_intrinsic_value(stock_quote):
    """估算内在价值（2026-08 升级：结合最新季报 + 动态行业PE）

    EPS 优先取最新财报并按报告期年化（年报×1 / 三季报×4/3 / 中报×2 / 一季报×4），
    取不到财报才退回 价格/PE 反推；
    行业PE 优先东财动态板块PE，失败退回 INDUSTRY_PE_MAP 静态映射表。
    内在价值 = 年化EPS × 行业PE × 0.8（安全边际）
    """
    pe = stock_quote.get("pe", 25) or 25
    price = stock_quote.get("price", 0)
    code = stock_quote.get("code", "")

    ind = get_industry_pe(code)
    industry_pe = ind["pe"]
    industry_name = ind.get("industry", "未知")

    # EPS：优先最新财报（按报告期年化）
    eps, eps_source = 0, "none"
    fin = get_latest_financials(code)
    if fin and fin.get("eps"):
        try:
            eps = float(fin["eps"])
            month = int((fin.get("report_date") or "12")[5:7])
            annual_factor = {3: 4, 6: 2, 9: 4 / 3, 12: 1}.get(month, 1)
            eps = round(eps * annual_factor, 3)
            eps_source = "quarterly_report"
        except Exception:
            eps = 0
    if not eps and pe and price:
        eps = round(price / pe, 3)
        eps_source = "price_pe_derived"

    intrinsic = round(eps * industry_pe * 0.8, 2) if eps else price
    return {
        "price": price, "intrinsic_value": intrinsic,
        "gap": round((price - intrinsic) / intrinsic * 100, 1) if intrinsic else 0,
        "pe": pe, "industry_pe": industry_pe,
        "industry_name": industry_name,
        "eps": eps, "eps_source": eps_source,
        "financials": fin,
    }


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
