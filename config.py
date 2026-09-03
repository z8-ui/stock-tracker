"""
配置文件 - 相当于 application.properties
所有可调参数集中在这里，改配置不用改代码
"""

# Flask 服务器配置
SERVER_HOST = "0.0.0.0"   # 允许局域网访问
SERVER_PORT = 5000
# ⚠️ 安全默认值: 仓库内 DEBUG=False(公网/局域网暴露时无 Werkzeug 调试器 RCE 风险)
#    本地开发想开调试(自动重载+错误堆栈), 在 config_local.py 里加一行 DEBUG = True 即可
DEBUG = False

# 页面标题
APP_NAME = "A股追踪系统"

# AI 分析 - DeepSeek API key(个人配置, 放 config_local.py, 不会上传 GitHub)
DEEPSEEK_API_KEY = ""

# AI 分析接口可选 token(防滥用): 非空时 /api/ai-analysis 必须带 ?token=xxx
# 自用可留空(不校验); 部署到公网时建议在 config_local.py 设置一个随机串
AI_API_TOKEN = ""

# 板块热力图 - 要显示的行业板块列表
# 东方财富行业板块代码，想加板块就在这加
SECTORS = [
    "银行", "保险", "证券", "房地产",
    "半导体", "软件开发", "通信设备",
    "汽车整车", "汽车零部件", "锂电池",
    "白酒", "食品饮料", "医药商业",
    "煤炭", "钢铁", "有色金属",
    "电力", "光伏设备", "军工装备",
    "家电", "纺织服装", "商业百货"
]

# 个股资金追踪 - 预设关注的个股（东方财富代码）
# 格式: "股票名": "股票代码"
# 个股资金追踪 - 预设关注的个股
# 警告：请勿在此处填写你的自选池！
# 创建 config_local.py 来保存个人配置，此文件会上传 GitHub
STOCK_WATCHLIST = {}

# 估值偏离 - 预设关注的个股
# 同上，个人配置请写在 config_local.py
VALUATION_WATCHLIST = {}

# 颜色主题
COLORS = {
    "上涨": "#ef5350",     # 红
    "下跌": "#26a69a",     # 绿
    "平盘": "#9e9e9e",     # 灰
    "主力净流入": "#ef5350",
    "主力净流出": "#26a69a",
    "板块热力_高": "#ef5350",
    "板块热力_中": "#ffb74d",
    "板块热力_低": "#26a69a",
    "背景": "#1a1a2e",
    "卡片背景": "#16213e",
    "文字主色": "#e0e0e0"
}

# ECharts 热力图色阶
HEATMAP_COLORS = ["#26a69a", "#66bb6a", "#ffb74d", "#ff8a65", "#ef5350", "#d32f2f"]

# 个股资金流向的维度名称
MONEY_FLOW_DIMS = ["超大单净流入", "大单净流入", "中单净流入", "小单净流入"]

# ==============================================================
#  本地配置覆盖（自选池等，非公开）
#  每个开发者创建自己的 config_local.py，已被 .gitignore 排除
# ==============================================================
try:
    from config_local import *  # noqa: F401, F403
except ImportError:
    # 没有本地配置时使用空列表，别人 fork 不会看到你的自选
    STOCK_WATCHLIST = {}
    VALUATION_WATCHLIST = {}
