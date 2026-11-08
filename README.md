<div align="center">

# A股追踪系统 · Stock Market Tracker

**零成本 · 无需外部数据库 · 免 API Key 的 A 股实时数据可视化系统**

浏览器打开即用：资金流向 / 板块热力 / 估值分析 / AI 技术分析

> 政策事件用本地 SQLite（`data/events.db`，不入库）；只有 Flask + requests 两个依赖。

</div>

## ✨ 功能一览

| 模块 | 说明 |
|------|------|
| 📊 **全市资金走向** | 大盘指数实时行情 + 主力/超大单/大单/中单/小单资金流向 |
| 🗺️ **板块热力图** | 22 个行业板块涨跌幅 Treemap + 资金流入排行 |
| 💰 **个股资金追踪** | 搜索 A 股任意个股：实时行情 + 近 20 日资金流向趋势 + 实时主力动向 |
| ⚖️ **估值偏离分析** | PE/PB 与**行业实时 PE**（东财板块实时数据）对比 + 内在价值估算 + **政策与事件（自动检索）** |
| 📰 **财报 × 政策** | 最新季报（营收/净利同比/EPS/ROE）+ 个股政策事件（直接/业务相关/行业间接/待确认四档，带来源与证据） |
| 🩺 **数据健康** | 首页底部：行情/资金/财报/估值/政策/AI/缓存/外部源 十项状态（REAL/CACHED/STALE/PARTIAL/UNAVAILABLE），只读缓存不发请求 |
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
├── app.py               # 入口: create_app() + 统一鉴权守卫
├── config.py            # 公共配置(可调参数)
├── config_local.py      # 个人配置(自选池/API Key, 不入库) ← 自行创建，可选
├── routes.py            # 路由层(25 个路由: 页面 5 + API 17 + 静态/健康检查)
├── data_service.py      # 数据层(缓存/single-flight/降级/多数据源/数据源统计)
├── chart_builder.py     # 图表层(原始数据 → ECharts JSON)
├── technical.py         # 技术指标(EMA/ZigZag/支撑压力/趋势线/斐波那契)
├── ai_analysis.py       # DeepSeek AI 分析(K线+指标 → 报告)
├── stock_profile.py     # 个股画像(东财 F10: 行业链/经营范围/关键词+证据, 6h 缓存)
├── policy_live.py       # 实时政策检索(公告+快讯, 四档归因, success/empty/partial/failed)
├── policy_notes.py      # 静态政策字典(仅作 seed 源与 DB 回退)
├── events_store.py      # SQLite 事件库(events / event_links / feed_seen)
├── event_feed.py        # 快讯采集(东财 + 新浪)
├── data_health.py       # 数据健康快照(只读缓存与 DB, 零外部请求)
├── market_calendar.py   # 交易日历
├── templates/           # base/dashboard/market_flow/heatmap/stock_flow/valuation/ai
├── static/js/           # format.js(格式化) + app.js(总览/热力/数据健康) + valuation.js(估值+政策+画线)
├── scripts/             # events_cli.py(事件库CLI) + package_release.py(打包+密钥扫描)
├── tests/               # 456 条离线单测（2026-11-08 实测）
└── requirements.txt     # 仅 flask + requests
```

> 没有独立「政策事件」页面和 `/events` 路由：政策事件统一在
> **估值偏离 → 个股估值 → 政策与事件** 里按个股展示（导航栏只有 5 个入口）。

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

> 核心功能**免 API Key、无需外部数据库服务、零 Node.js**；政策事件用本地 SQLite（首次运行自动建表）。
> 可选：想用 AI 分析，创建 `config_local.py` 写入
> `DEEPSEEK_API_KEY = "sk-..."` 即可（该文件已被 .gitignore 排除，不会误传 GitHub）。

## 🧪 测试与打包

```bash
# 单元测试（456 个用例，纯离线，不依赖 pytest；需先 pip install -r requirements.txt）
# 覆盖：Flask 路由/认证、缓存 SWR、政策检索与关联、估值口径、打包密钥扫描、
#       前端 JS（node 实跑 format.js / valuation.js 竞态）
python3 -m unittest discover -s tests -v

# 打发布包（排除 .git/.venv/__pycache__/config_local.py/*_cache.json，
# 并在入包前 + 出包后各扫一次密钥，命中即中止）
python3 scripts/package_release.py --dry-run
python3 scripts/package_release.py          # 输出到 dist/
```

数据诚信相关的开关（`config.py`，可用 `config_local.py` 覆盖）：

| 配置 | 默认 | 说明 |
|------|------|------|
| `ALLOW_MOCK_FALLBACK` | `False` | 行情接口失败时**不**用随机数编造数据，页面显示"暂无数据"；做离线演示才改 `True`（页面会标"模拟数据"） |
| `DEBUG` | `False` | `True` 时服务强制只绑 `127.0.0.1`（Werkzeug 调试器等于任意代码执行入口，不随 `0.0.0.0` 暴露） |
| `SERVER_HOST` | `127.0.0.1` | 默认只监听本机；改成局域网 IP 时必须同时设置 `SERVER_AUTH_TOKEN`，否则 `app.py` 拒绝启动（exit 2） |
| `SERVER_AUTH_TOKEN` | 空 | 非空且绑定非回环地址时启用 HTTP Basic（用户名 `stock`），密码即 token |
| `AI_API_TOKEN` | 空 | 非空时 `/api/ai-analysis` 只认 Header `X-AI-Token`（`hmac.compare_digest`）；**为空时 AI 接口仅允许本机**（127.0.0.1），局域网一律 403。不接受 URL query |
| `AI_CACHE_TTL_SEC` | `21600` | AI 分析缓存有效期（6 小时）；过期或 `refresh=1` 才重新调模型 |
| `VALUATION_SAFETY_MARGIN` | `0.8` | 内在价值的安全边际系数，接口会回显在 `model_params`，页面显示"安全边际 0.8" |
| `STALE_AFTER_SEC` | `900` | 盘中缓存超过 15 分钟标 `_stale`；休盘放宽到 3 天（超过就不再当"最近收盘"） |

估值口径说明：个股 PE 缺失（亏损/停牌）时返回 `pe=null` + `level="数据不可用"`，
**不再**用行业 PE 顶替（顶替会让偏离度恒为 0、结论永远"合理"）；
EPS 取不到时内在价值返回 `null`，不再用股价顶替。

## 📊 数据可信度（每条曲线的出处）

页面上的每个数字都能追到来源、刷新节奏和失败行为：

| 页面 / 指标 | 数据来源 | 刷新 | 失败时 |
|---|---|---|---|
| 个股行情（价、涨跌、成交额、PE/PB） | 腾讯 `qt.gtimg.cn` | 内存 8 秒；休市取文件快照 | 显示"暂无数据" |
| 个股 20 日资金流（超大/大/中/小单） | 东财 `fflow/daykline`（真实逐日） | 5 分钟 | 显示"暂无数据"，**不再随机填充** |
| 个股当日资金净流入 + 占比 | 东财 `fflow/kline` + 腾讯成交额 | 每次请求 | 卡片隐藏 |
| 板块资金热力图 | 东财 `clist`（f62 主力净额） | 8 秒 | 空图（只有 `ALLOW_MOCK_FALLBACK=True` 才用模拟） |
| 行业平均 PE | 东财 `f127 → suggest BK → f9` 三步链 | 1 小时 | 降级静态映射表，页面标注"静态参考" |
| 板块估值（PE/PB 气泡） | 东财 `clist` | 每次请求 | PE ≤ 0（亏损/缺值）的板块剔除，不参与对比 |
| 估值偏离 | 腾讯 PE + 行业 PE | 行情 8 秒 / 行业PE 1 小时 | PE 缺失 → "数据不可用"，**不拿行业 PE 顶替** |
| 内在价值（估算） | 最新季报 EPS × 行业 PE × 0.8 | 财报 6 小时 | EPS 缺失或亏损 → `null`，**不拿股价顶替** |
| 指数 K 线 / EMA / 斐波那契 | 腾讯 `ifzq.gtimg.cn` | 每次请求 | 报错并提示，不模拟 |
| AI 技术分析 | DeepSeek（可选，自备 key） | 本地缓存，`refresh=1` 强制重算 | 失败结果不写缓存，避免错误被反复展示 |

三条口径约定（2026-11 整改后）：

1. **抓取时间来自磁盘。** 缓存结构是 `{__data, fetched_at}`，重启后不会把几天前的快照标成"刚刚获取"；旧格式缓存条目会显示"抓取时间未知"，不猜。
2. **模拟数据必须可识别。** 任何降级分支返回的数都带 `is_mock=true`，图表副标题显示"⚠️ 模拟数据（非真实成交）"；默认 `ALLOW_MOCK_FALLBACK=False`，接口挂了就显示"暂无数据"。
3. **"算不出来"就说算不出来。** 亏损股的 PE、缺失的 EPS 一律返回 `null` + 原因，不用行业均值或股价凑一个"合理"出来——宁可空白，不给假结论。

## 📰 政策事件（SQLite，整合在个股估值页）

估值页的「政策与事件」原来读 `policy_notes.py` 里的硬编码字典——加一条要改代码、重启服务，
没来源 URL、没状态，同一条事件要在 4 只股票里各抄一份文案。现在改为 SQLite 两张表：

```
events        事件本体：event_date / title / summary / direction / source / source_url / fingerprint / status
event_links   关联：target_type = stock | industry，target_key = 股票代码或行业名
```

一条「美国禁止废钨出口」只存一份文案，挂 4 只钨股 + 1 个行业（`event_links` 5 行），改一次全站生效。
去重靠 `fingerprint = sha1(日期|标题)`，同一事件换个标的重录不会多一行。

```bash
python3 scripts/events_cli.py seed                       # 把老字典导入（幂等 + 单事务，重复跑不重复；新库 seed 后 4 事件 / 15 关联）
python3 scripts/events_cli.py stats                      # 库内统计
python3 scripts/events_cli.py list --code 000657          # 某只股票挂的事件
python3 scripts/events_cli.py add --date 2026-11-07 --title "…" --summary "…" \
        --direction 利好 --source "财联社" --stock 000657 --industry 小金属
python3 scripts/events_cli.py add-text --file news.txt --stock 000657   # 粘新闻→模型抽→预览
python3 scripts/events_cli.py add-text --file news.txt --stock 000657 --yes   # 确认写库
python3 scripts/events_cli.py archive --id 5             # 归档（不删数据，页面立即不显示）
```

两条规则：

1. **读写分离。** 页面永远先读 DB；DB 不存在/为空/出错时自动回退 `policy_notes.py` 静态字典，
   DB 挂了页面不会变空白。所以 `policy_notes.py` 现在只是 fallback + seed 源，不用再手改。
2. **AI 只做初稿。** `add-text` 调 DeepSeek 抽成 JSON 后只打印预览，加 `--yes` 才写库；
   提示词明确要求「原文没有的数字一律不要编」。实测一条新闻：242 prompt + 78 completion token。

库文件 `data/events.db`，已加入 `.gitignore`（本地数据不入库）。

### 半自动采集：feed

```bash
python3 scripts/events_cli.py feed                    # 抓东财快讯 + 新浪财经 → 关键词粗筛 → 打印候选
python3 scripts/events_cli.py feed --strong           # 只看政策/行业/自选股相关（丢掉公司面消息）
python3 scripts/events_cli.py feed --pick 2,5 --stock 000657 --industry 小金属   # 预览抽取结果
python3 scripts/events_cli.py feed --pick 2,5 --stock 000657 --industry 小金属 --yes   # 确认写库
```

- 两个源实测可用：东财快讯（JSONP，50 条/次，digest 即全文）、新浪财经（JSON，20 条/次，只有摘要）。
  财联社 telegraph 接口需要 sign，直连 404，**没有硬编码 token 绕过**。
- 分两级：`!` 强相关（出口管制/配额/关税/行业词/自选股代码命中）、`·` 弱相关（涨价/订单/立案/减持等公司面）。
- 近似标题自动合并（"000520，被立案" / "000520，被证监会立案" → 留信息更全的一条，标 `x2`）。
- 看过的候选记在 `feed_seen` 表，第二天不会再问同一批；`--all` 可回看，`--dry-run` 不记录。
- **没有全自动入库**：AI 只出初稿，`--yes` 才写库。

### 页面上的位置：估值偏离 → 个股估值 → 政策与事件

选中个股后自动完成：F10 个股画像（行业链 / 经营范围 / 关键词+证据）→ 行业名解析
（请求参数 → 东财板块 → F10 链尾）→ 库内已审核事件（两套行业分类都查）→ 实时检索公告与快讯
→ 去重 → 按关联强度分组（直接 / 业务相关 / 行业间接 / 待确认）。

- 检索状态四档分明：`success` / `empty` / `partial` / `failed`；**取不到 ≠ 没有政策**，页面分开提示。
- 每条事件都带 `source_url`、`review_status`、`relation_basis`、`evidence`；
  live 检索结果一律 `pending` + `direction=待确认`，不进结论。
- 切换股票时旧请求立即作废（序号 + AbortController 双保险），不会出现"看 A 股显示 B 股政策"。
- **没有独立政策页面**：导航栏不含「政策事件」入口，`/events` 与 `/api/event-timeline` 已下线。

ECharts 已 vendor 到 `static/vendor/echarts.min.js`（1.0 MB）：原来只从 jsdelivr CDN 加载，
在被墙/断网环境里 `echarts is not defined` 会让整页图表空白且后续渲染中断。现在本地优先、CDN 兜底，
图表库缺失时只有图表降级、表格照常显示。

## 🩺 数据健康（/api/data-health）

首页底部新增一块只读面板，回答"这些数字到底有多新"：

| 监控项 | 依据 |
|---|---|
| 市场资金 / 板块资金 / 个股历史资金 / 财报 / 行业估值 | 读 `_data_cache.json` 的 `fetched_at` 与 `_source`，按时间分档 |
| 政策事件 | 本地 SQLite：总条数、待审核数、最近采集时间 |
| AI 分析 | 当前模型名（不回显 key）+ 是否有分析记录 |
| 缓存系统 | 条目数、过期数、最老一份多久 |
| 外部数据源 | 进程内成功率、平均耗时、最近错误 |

状态口径：`REAL`(15 分钟内) / `CACHED`(1 天内) / `STALE`(过期或时间未知) /
`PARTIAL`(部分可用) / `UNAVAILABLE`(不可用)。

三条硬约束：**不发任何外部请求**（只读缓存与 DB）；**单模块失败不牵连全局**；
**缓存绝不标成实时**（没有抓取时间就写 STALE 并说明"时间未知"）。

## 🔌 API 路由（与代码一致，25 个）

| 路由 | 说明 |
|---|---|
| `/` `/market-flow` `/heatmap` `/stock-flow` `/valuation` `/ai` | 6 个页面（导航栏 5 个入口 + AI） |
| `/api/health` `/api/ready` | 进程存活 / 数据是否硬过期（503） |
| `/api/market-flow` `/api/heatmap` | 全市资金、板块热力 |
| `/api/stock-quote` `/api/stock-flow` `/api/stock-real-flow` | 个股行情 / 20 日资金 / 当日主力 |
| `/api/valuation` `/api/intrinsic-value` `/api/sector-valuation` | 估值偏离 / 内在价值 / 板块估值 |
| `/api/policy-events` | 个股政策与事件（`live=1` 实时检索，`refresh=1` 清缓存） |
| `/api/data-health` | 数据健康快照（只读，不联网） |
| `/api/index-analysis` `/api/fibonacci` `/api/swing-analysis` | 画线分析三件套 |
| `/api/ai-config` `/api/ai-analysis` | AI 配置状态 / 分析（可选，带 token 与限流） |
| `/api/search-stock` | 个股搜索 |

## 📡 数据来源（全部免费公开接口）

| 接口 | 用途 | 稳定性 |
|------|------|--------|
| 腾讯行情 qt.gtimg.cn | 个股/指数实时行情 | ⭐⭐⭐⭐⭐ |
| 腾讯历史K线 web.ifzq.gtimg.cn | 指数/个股K线（EMA/斐波那契/画线） | ⭐⭐⭐⭐⭐ |
| 东方财富 push2.eastmoney.com | 板块资金流向 / 行业实时PE / F10财报 | ⭐⭐⭐ |
| 东方财富 fflow/daykline | 个股逐日资金流（近 20 日真实曲线） | ⭐⭐⭐⭐ |
| DeepSeek API | AI 分析（可选，自备 key） | ⭐⭐⭐⭐ |

## ⚠️ 免责声明

本项目所有数据来自公开网络接口，仅供**学习与技术交流**，不构成任何投资建议。
股市有风险，入市需谨慎；据此操作，风险自负。

## 📄 License

[MIT](LICENSE) © 2026 [z8-ui](https://github.com/z8-ui)

## 🔐 访问控制（V2.0.1 收尾）

| 场景 | 行为 |
|---|---|
| 本机 127.0.0.1 / ::1 | 免认证（本机用户本来就能读配置/起进程） |
| 局域网/公网访问 | 必须 HTTP Basic（用户名 `stock`，密码 `SERVER_AUTH_TOKEN`）；未配置 token → 请求 403 |
| 配置 `SERVER_HOST` 为非回环且无 token | 拒绝启动（import 即抛错，`python app.py` 退出码非零） |
| AI 接口 | 优先 `X-AI-Token`；否则接受已通过 Basic 的会话；都没有时仅本机 |
| 启动入口 | `python app.py` / `flask --app app run` / `gunicorn app:app` 共用同一守卫（装在 `create_app()`） |

⚠️ Basic 只是 base64 编码，必须放在 HTTPS 反向代理之后，不要把 5000 端口直接映射到公网。

## 🗓 事件时间字段（V2.0.1 收尾）

- `occurred_at`：事件实际发生时间，原文没写就是 `NULL`（**绝不补今天**）
- `published_at`：来源发布时间，可能只有日期
- `collected_at`：系统采集时间
- 估值页/事件页：发生时间未知时显示“发布 YYYY-MM-DD（发生时间未知）”或“日期未知”
