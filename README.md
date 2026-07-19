# A股追踪系统 · Stock Market Tracker

一个轻量级的 **A 股实时数据可视化系统**，无需数据库、无需付费 API，浏览器打开即用。

**项目背景**：自学 Python 全栈开发的练手项目，从零搭建，前后端分离架构，覆盖了数据采集、图表可视化、本地持久化等完整链路。

---

## 功能一览

| 功能 | 说明 |
|------|------|
| **全市资金走向** | 大盘指数实时行情 + 主力/超大单/大单/中单/小单资金流向 |
| **板块热力图** | 30个行业板块涨跌幅 Treemap + 资金流入排行 |
| **个股资金追踪** | 搜索 A 股任意个股，查看实时行情 + 近20日资金流向趋势 |
| **估值偏离分析** | 个股 PE/PB 与行业平均对比 + 股价 vs 内在价值估算 |
| **板块估值** | 行业板块 PE/PB 散点图（气泡大小=市值） |
| **科创50 EMA20** | 20日指数移动平均线偏离度趋势 + 斐波那契回调线 |
| **自选股池** | 搜索历史 + 自选股池 (localStorage 持久化，跨页面共享) |
| **自动刷新** | 总览/资金/板块 30s 自动刷新，个股行情 15s 刷新 |

---

## 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| **后端** | Python + Flask | 轻量 Web 框架，单文件启动 |
| **数据采集** | 腾讯行情 API + 东方财富 API | 免费公开接口，零成本 |
| **前端** | HTML + CSS + JavaScript | 原生三件套，无框架依赖 |
| **图表** | ECharts 5 (CDN) | 阿里开源图表库 |
| **存储** | localStorage | 自选股/搜索历史持久化 |
| **部署** | WSL / Linux / Mac | 任意 Python 环境直接运行 |

---

## 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/<你的用户名>/stock-tracker.git
cd stock-tracker

# 2. 安装依赖
pip install flask requests

# 3. 启动
python app.py

# 4. 浏览器打开
# http://localhost:5000
```

> 仅需 `flask` 和 `requests` 两个依赖，无需数据库，无需 Node.js，无需 API Key。

---

## 项目结构

```
stock-tracker/
├── app.py                 # 应用入口（main）
├── config.py              # 配置文件（股票列表、颜色主题）
├── routes.py              # 路由层（URL → 数据 → 页面）
├── data_service.py        # 数据层（API 请求、模拟、缓存）
├── chart_builder.py       # 图表层（原始数据 → ECharts JSON）
├── requirements.txt       # 依赖清单
├── run.sh                 # 启动脚本
├── templates/             # 页面模板
│   ├── base.html          #   框架页（导航栏）
│   ├── dashboard.html     #   总览仪表盘
│   ├── market_flow.html   #   资金走向
│   ├── heatmap.html       #   板块热力
│   ├── stock_flow.html    #   个股资金
│   └── valuation.html     #   估值分析（3个Tab）
└── static/
    ├── css/style.css      # 深色主题样式
    └── js/app.js          # 图表渲染 + 自动刷新
```

### 架构说明（类似 MVC）

```
浏览器请求 URL
    ↓
routes.py  ←→  data_service.py（API 数据）
    ↓                    ↓
    templates/    chart_builder.py（图表 JSON）
    ↓
浏览器渲染（ECharts）
```

- **routes.py** = Controller：收到请求，调数据层，返回页面
- **data_service.py** = DAO：只做数据获取，不关心展示
- **chart_builder.py** = Service：加工数据成图表格式
- **templates/** = View：页面模板

---

## 数据来源

| API | 用途 | 是否免费 | 稳定性 |
|-----|------|---------|--------|
| [腾讯行情](http://qt.gtimg.cn/) | 个股/指数实时行情 | ✅ 免费 | ⭐⭐⭐⭐⭐ |
| [腾讯历史K线](http://web.ifzq.gtimg.cn/) | 指数历史数据（EMA/斐波那契） | ✅ 免费 | ⭐⭐⭐⭐⭐ |
| [东方财富](https://push2.eastmoney.com/) | 板块资金流向 | ✅ 免费 | ⭐⭐⭐（非交易时段受限） |
| [新浪财经](https://hq.sinajs.cn/) | 个股估值数据 | ✅ 免费 | ⭐⭐⭐⭐ |

---

## 简历亮点

- **全栈能力**：从数据采集 → 后端处理 → 前端可视化，一人完成全链路
- **架构设计**：分层架构（MVC 风格），便于扩展和维护
- **异常处理**：多数据源冗余（东方财富受限时自动切换腾讯+模拟兜底）
- **用户体验**：自动刷新、搜索历史、自选股池（localStorage）、响应式布局
- **零成本**：不依赖任何付费 API 或云服务

---

## License

MIT
