"""
配置文件 - 相当于 application.properties
所有可调参数集中在这里，改配置不用改代码
"""

# Flask 服务器配置
# ⚠️ 默认只绑回环地址：0.0.0.0 会把看板暴露给整个局域网，而这个项目没有账号体系。
#    确实要给局域网/手机看时，改这里或 config_local.py，但必须同时设置 SERVER_AUTH_TOKEN，
#    否则 app.py 会直接拒绝启动（见 app.py:lan_auth_error）。
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000
# 局域网访问的 HTTP Basic 密码（用户名固定 stock）。为空 = 只允许本机。
SERVER_AUTH_TOKEN = ""
# ⚠️ 安全默认值: 仓库内 DEBUG=False(公网/局域网暴露时无 Werkzeug 调试器 RCE 风险)
#    本地开发想开调试(自动重载+错误堆栈), 在 config_local.py 里加一行 DEBUG = True 即可
DEBUG = False

# 行情接口失败时，是否允许用随机数编造数据兜底
# ⚠️ 默认 False：宁可页面显示「暂无数据」，也不把模拟数据当真实数据展示
#    （做离线演示时可以临时改成 True，页面上会带「模拟数据」标记）
ALLOW_MOCK_FALLBACK = False

# 页面标题
APP_NAME = "A股追踪系统"

# AI 分析 - DeepSeek API key(个人配置, 放 config_local.py, 不会上传 GitHub)
DEEPSEEK_API_KEY = ""

# AI 分析接口可选 token(防滥用): 非空时 /api/ai-analysis 必须带 Header `X-AI-Token: <token>`
# ⚠️ 不接受 ?token=xxx：URL 会进浏览器历史、代理日志、Flask access log
# 自用可留空(不校验); 部署到公网时建议在 config_local.py 设置一个随机串
AI_API_TOKEN = ""
# AI 分析结果缓存有效期（秒）。过期后重新调用模型；refresh=1 无视缓存。
AI_CACHE_TTL_SEC = 6 * 3600

# 估值模型参数（拍脑袋的系数要能看见、能改）
# 内在价值 = 年化 EPS × 行业 PE × VALUATION_SAFETY_MARGIN
VALUATION_SAFETY_MARGIN = 0.8
# 缓存超过这个秒数就标记 _stale=True（前端会显示"缓存数据"），避免旧数据被当成实时
STALE_AFTER_SEC = 900

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
