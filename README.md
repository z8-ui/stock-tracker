<div align="center">

# A股追踪系统 · Stock Market Tracker

**零成本 · 零数据库 · 免 API Key 的 A 股实时数据可视化系统**

浏览器打开即用：资金流向 / 板块热力 / 估值分析 / AI 技术分析

</div>

## ✨ 功能一览

| 模块 | 说明 |
|------|------|
| 📊 **全市资金走向** | 大盘指数实时行情 + 主力/超大单/大单/中单/小单资金流向 |
| 🗺️ **板块热力图** | 22 个行业板块涨跌幅 Treemap + 资金流入排行 |
| 💰 **个股资金追踪** | 搜索 A 股任意个股：实时行情 + 近 20 日资金流向趋势 + 实时主力动向 |
| ⚖️ **估值偏离分析** | PE/PB 与**行业实时 PE**（东财板块实时数据）对比 + 内在价值估算 |
| 📰 **财报 × 政策提醒** | 最新季报（营收/净利同比/EPS/ROE）+ 政策/地缘事件时间线，估值不再只看数字 |
| 🧮 **板块估值** | 行业板块 PE/PB 散点图（气泡大小 = 市值） |
| 🤖 **AI 技术分析** | K线 + 技术指标 → DeepSeek 生成个股分析报告（可选功能，带限流与 token 防护） |
| 📉 **画线分析** | 科创50 EMA20 偏离度 / 斐波那契回调 / **ZigZag 摆动点 + 支撑压力 + 趋势线**（日线/周线） |
| ⭐ **自选股池** | 搜索历史 + 自选股池（localStorage，跨页面共享） |
| 🔄 **自动刷新** | 总览/资金/板块 30s，个股行情 15s；休市时段自动降频 |

## 🛡️ 可靠性设计（生产级细节）

> 免费公开行情接口在非交易时段/波动剧烈时经常抽风，本项目做了全套容错：

- **三级缓存**：内存(8s TTL) → 文件(交易日快照) → 实时抓取，任何一层失败自动降级
- **SWR 后台刷新 + single-flight**：页面永不白屏；同 key 并发请求合并为一次网络调用
- **数据诚信标注**：每个响应自动注入 `_asof`(数据时间) / `_source`(实时/缓存/上一交易日)，前端明示数据新旧
- **健康检查分离**：`/api/health`(进程活着) vs `/api/ready`(数据硬过期时 503)，便于挂监控
- **多市场容错**：A股/港股/指数/基金行情，东方财富受限时自动切腾讯 + 模拟兜底
- **AI 接口防护**：可选 token 校验 + 每分钟限流，防公网滥用

## 🏗️ 架构（分层 MVC 风格）

```
routes.py (Controller)          data_service.py (DAO)
  路由/参数/鉴权/限流      ←→   腾讯/东财/新浪 API、三级缓存、降级
        ↓                            ↓
  templates/ (View)          chart_builder.py · technical.py · ai_analysis.py
  Jinja2 + ECharts             图表JSON · EMA/ZigZag/斐波那契 · DeepSeek 分析
        ↓
  浏览器渲染 (ECharts 5)
```

- **config.py** = 唯一配置点（端口/主题/板块列表）；个人自选池与 DeepSeek Key 放
  `config_local.py`（已 gitignore，fork 不泄露）
- **policy_notes.py** = 「策池」：政策/地缘事件按时间线维护，估值页自动展示

### 目录结构

```
stock-tracker/
├── app.py               # 入口: Flask + 注册 blueprint
├── config.py            # 公共配置(可调参数)
├── config_local.py      # 个人配置(自选池/API Key, 不入库) ← 自行创建
├── routes.py            # 路由层(含 AI 限流/token 防护)
├── data_service.py      # 数据层(缓存/single-flight/降级/多数据源)
├── chart_builder.py     # 图表层(原始数据 → ECharts JSON)
├── technical.py         # 技术指标(EMA/ZigZag/支撑压力/趋势线/斐波那契)
├── ai_analysis.py       # DeepSeek AI 分析(K线+指标 → 报告)
├── policy_notes.py      # 政策/事件时间线(策池)
├── templates/           # base/dashboard/market_flow/heatmap/
│                        # stock_flow/valuation/ai
├── static/js/           # app.js(总览) + valuation.js(估值+画线)
└── requirements.txt     # 仅 flask + requests
```

## 🚀 快速开始

```bash
# 1. 克隆
git clone https://github.com/z8-ui/stock-tracker.git
cd stock-tracker

# 2. 安装依赖（仅两个）
pip install -r requirements.txt

# 3. 启动
python app.py

# 4. 打开 http://localhost:5000
```

> 核心功能**零 API Key、零数据库、零 Node.js**。
> 可选：想用 AI 分析，创建 `config_local.py` 写入
> `DEEPSEEK_API_KEY = "sk-..."` 即可（该文件已被 .gitignore 排除，不会误传 GitHub）。

## 📡 数据来源（全部免费公开接口）

| 接口 | 用途 | 稳定性 |
|------|------|--------|
| 腾讯行情 qt.gtimg.cn | 个股/指数实时行情 | ⭐⭐⭐⭐⭐ |
| 腾讯历史K线 web.ifzq.gtimg.cn | 指数/个股K线（EMA/斐波那契/画线） | ⭐⭐⭐⭐⭐ |
| 东方财富 push2.eastmoney.com | 板块资金流向 / 行业实时PE / F10财报 | ⭐⭐⭐ |
| DeepSeek API | AI 分析（可选，自备 key） | ⭐⭐⭐⭐ |

## ⚠️ 免责声明

本项目所有数据来自公开网络接口，仅供**学习与技术交流**，不构成任何投资建议。
股市有风险，入市需谨慎；据此操作，风险自负。

## 📄 License

[MIT](LICENSE) © 2026 [z8-ui](https://github.com/z8-ui)
