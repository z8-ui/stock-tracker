"""
图表数据构建层 - 相当于 Service
把原始数据拼成 ECharts 前端需要的 JSON 格式
"""

from config import COLORS, HEATMAP_COLORS
from datetime import datetime


def build_market_flow_chart(flow_data):
    """
    全市资金走向 → 饼图 + 柱状图的数据
    """
    if not flow_data:
        return None

    items = [
        # 中单/小单可能是 None（源没给金额）——原样透出，前端显示 "--"，不补 0
        {"name": "超大单", "value": flow_data.get("super_large")},
        {"name": "大单", "value": flow_data.get("large_order")},
        {"name": "中单", "value": flow_data.get("middle_order")},
        {"name": "小单", "value": flow_data.get("small_order")},
    ]
    # 交易日：来自 get_market_flow() 解析的腾讯行情（原样 8 位，如 20261106）。
    # 以前只拼进 title，前端读 data.date 永远 undefined；这里原样透出，不生成任何日期。
    trade_date = flow_data.get("date") or ""
    return {
        "title": f"全市资金流向（{trade_date}）" if trade_date else "全市资金流向",
        "date": trade_date,
        "main_force": flow_data.get("main_force", 0),
        "index_name": flow_data.get("index_name", "上证指数"),
        "index_price": flow_data.get("index_price", 0),
        "index_change": flow_data.get("index_change", 0),
        "items": items,
        # value 为 None 时不能直接比大小（会 TypeError）；None 按"流出色"渲染，不影响数值
        "colors": [COLORS["主力净流入"] if (i["value"] or 0) > 0 else COLORS["主力净流出"]
                   for i in items]
    }


def build_heatmap_chart(sectors):
    """
    板块数据 → 热力图数据
    返回 ECharts treemap / scatter 格式
    """
    if not sectors:
        return None

    tree_data = []
    for s in sectors:
        flow = s["flow"]
        change = s["change"]

        # 双向热力图：颜色按资金流向方向标色
        # 流入 → 红/橙色系，流出 → 绿/蓝冷色系
        if flow > 3:
            color = "#ef5350"        # 强流入 → 深红
        elif flow > 1:
            color = "#ff8a65"        # 中流入 → 浅红
        elif flow > 0:
            color = "#ffb74d"        # 弱流入 → 橙
        elif flow > -1:
            color = "#66bb6a"        # 弱流出 → 浅绿
        elif flow > -3:
            color = "#26a69a"        # 中流出 → 绿
        else:
            color = "#00695c"        # 强流出 → 深绿

        tree_data.append({
            "name": s["name"],
            "value": abs(flow),       # 方块面积 = 资金规模绝对值
            "change": change,
            "flow": flow,
            "itemStyle": {"color": color}
        })

    return tree_data


def build_stock_flow_chart(stock_name, flow_data):
    """
    个股资金流向 → 双柱状图数据（净流入/流出）
    数据来自东方财富逐日资金流（单位亿元）；is_mock=True 时前端必须显式标注"模拟数据"
    """
    if not flow_data:
        return None

    dates = flow_data["dates"]
    f = flow_data["flows"]
    is_mock = bool(flow_data.get("is_mock"))

    # 转为正负值：净流入为正，净流出为负
    series = [
        {"name": "超大单", "data": [round(v, 2) for v in f["super_large"]]},
        {"name": "大单",   "data": [round(v, 2) for v in f["large"]]},
        {"name": "中单",   "data": [round(v, 2) for v in f["middle"]]},
        {"name": "小单",   "data": [round(v, 2) for v in f["small"]]},
    ]

    # 抓取时间（_cached_get 注入的 _asof 是 epoch 秒）
    asof_txt = ""
    try:
        asof_txt = datetime.fromtimestamp(flow_data["_asof"]).strftime("%m-%d %H:%M")
    except (KeyError, TypeError, ValueError, OSError):
        pass
    subtitle = "⚠️ 模拟数据（非真实成交）" if is_mock else "数据源：东方财富逐日资金流 · 亿元"
    if not is_mock:
        subtitle += f" · 抓取 {asof_txt}" if asof_txt else " · 抓取时间未知"

    return {
        "title": f"{stock_name} 资金流向（近{len(dates)}日）",
        "subtitle": subtitle,
        "dates": dates,
        "series": series,
        "colors": [COLORS["主力净流入"], COLORS["主力净流入"], COLORS["主力净流出"], COLORS["主力净流出"]],
        "is_mock": is_mock,
        "data_date": dates[-1] if dates else "",
    }


def build_valuation_chart(stock_name, val_data, industry_pe=25):
    """
    个股估值偏离 → 对比条图
    val_data: {"pe": 30, "pb": 5, "pe_ttm": 28}
    industry_pe: 行业平均 PE（后端直接拿不到就写固定值，后续可优化）
    2026-11: pe 为 None（亏损/停牌）时不再拿行业 PE 顶替，偏离度给 None，
    前端据此显示「数据不可用」而不是「合理」。
    """
    if not val_data:
        return None

    pe = val_data.get("pe")
    pb = val_data.get("pb", 0)
    pe_ttm = val_data.get("pe_ttm")

    if pe is None or not industry_pe:
        deviation_pe = None
        level = "数据不可用"
    else:
        # 偏离度计算: (个股PE - 行业平均PE) / 行业平均PE * 100%
        deviation_pe = round((pe - industry_pe) / industry_pe * 100, 1)
        # 偏离等级
        level = "偏高" if deviation_pe > 20 else ("偏低" if deviation_pe < -20 else "合理")

    return {
        "title": f"{stock_name} 估值分析",
        "date": val_data.get("date", ""),
        "pe": pe,
        "pb": pb,
        "pe_ttm": pe_ttm,
        "pe_ttm_note": val_data.get("pe_ttm_note", ""),
        "industry_pe": industry_pe,
        "deviation_pe": deviation_pe,
        "level": level,
        "available": pe is not None,
        "level_reason": val_data.get("level_reason", ""),
    }
