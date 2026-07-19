"""
图表数据构建层 - 相当于 Service
把原始数据拼成 ECharts 前端需要的 JSON 格式
"""

from config import COLORS, HEATMAP_COLORS


def build_market_flow_chart(flow_data):
    """
    全市资金走向 → 饼图 + 柱状图的数据
    """
    if not flow_data:
        return None

    items = [
        {"name": "超大单", "value": flow_data.get("super_large", 0)},
        {"name": "大单", "value": flow_data.get("large_order", 0)},
        {"name": "中单", "value": flow_data.get("middle_order", 0)},
        {"name": "小单", "value": flow_data.get("small_order", 0)},
    ]
    return {
        "title": f"全市资金流向（{flow_data.get('date', '')}）",
        "main_force": flow_data.get("main_force", 0),
        "index_name": flow_data.get("index_name", "上证指数"),
        "index_price": flow_data.get("index_price", 0),
        "index_change": flow_data.get("index_change", 0),
        "items": items,
        "colors": [COLORS["主力净流入"] if i["value"] > 0 else COLORS["主力净流出"] for i in items]
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
        # 颜色深浅按涨跌幅
        change = s["change"]
        if change > 3:
            color = HEATMAP_COLORS[4]
        elif change > 1:
            color = HEATMAP_COLORS[3]
        elif change > 0:
            color = HEATMAP_COLORS[2]
        elif change > -1:
            color = HEATMAP_COLORS[1]
        else:
            color = HEATMAP_COLORS[0]

        tree_data.append({
            "name": s["name"],
            "value": abs(s["flow"]),
            "change": change,
            "flow": s["flow"],
            "itemStyle": {"color": color}
        })

    return tree_data


def build_stock_flow_chart(stock_name, flow_data):
    """
    个股资金流向 → 双柱状图数据（净流入/流出）
    """
    if not flow_data:
        return None

    dates = flow_data["dates"]
    f = flow_data["flows"]

    # 转为正负值：净流入为正，净流出为负
    series = [
        {"name": "超大单", "data": [round(v, 2) for v in f["super_large"]]},
        {"name": "大单",   "data": [round(v, 2) for v in f["large"]]},
        {"name": "中单",   "data": [round(v, 2) for v in f["middle"]]},
        {"name": "小单",   "data": [round(v, 2) for v in f["small"]]},
    ]

    return {
        "title": f"{stock_name} 资金流向（近20日）",
        "dates": dates,
        "series": series,
        "colors": [COLORS["主力净流入"], COLORS["主力净流入"], COLORS["主力净流出"], COLORS["主力净流出"]]
    }


def build_valuation_chart(stock_name, val_data, industry_pe=25):
    """
    个股估值偏离 → 对比条图
    val_data: {"pe": 30, "pb": 5, "pe_ttm": 28}
    industry_pe: 行业平均 PE（后端直接拿不到就写固定值，后续可优化）
    """
    if not val_data:
        return None

    pe = val_data.get("pe", 0)
    pb = val_data.get("pb", 0)
    pe_ttm = val_data.get("pe_ttm", 0)

    # 偏离度计算: (个股PE - 行业平均PE) / 行业平均PE * 100%
    deviation_pe = round((pe - industry_pe) / industry_pe * 100, 1) if industry_pe else 0

    return {
        "title": f"{stock_name} 估值分析",
        "date": val_data.get("date", ""),
        "pe": pe,
        "pb": pb,
        "pe_ttm": pe_ttm,
        "industry_pe": industry_pe,
        "deviation_pe": deviation_pe,
        # 偏离等级
        "level": "偏高" if deviation_pe > 20 else ("偏低" if deviation_pe < -20 else "合理")
    }
