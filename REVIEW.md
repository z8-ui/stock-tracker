# 代码评审向导（REVIEW）

这份文件是给外部评审者（人或 AI）看的：说明这个项目想做什么、哪些地方是刻意取舍、
哪些地方我知道还没做好，以及**我希望你重点挑什么毛病**。

> 项目：A 股个股/板块数据看板 + 估值偏离 + 个股政策事件（本地 SQLite）
> 形态：单进程 Flask + 原生 JS + ECharts + SQLite，纯标准库单测，无前端构建
> 规模：约 6.0k 行 Python（不含测试）/ 1.6k 行 JS，**320 个离线单测**

> **当前版本 V2.0.2（2026-11-08）**
> 本轮（相对 V2.0.1）：
> 1. **政策事件只保留在个股估值里**。独立「政策事件」页、`/events` 页面路由、
>    `/api/event-timeline` 接口、`templates/events.html`、`static/js/events.js` 全部删除；
>    导航栏只剩 首页/资金流向/热力图/个股资金/估值偏离/AI分析 六个入口。数据层
>    （`events_store` / `event_feed` / `scripts/events_cli.py`）保留，因为估值页政策区块和每日采集还在用。
> 2. **行业解析顺序修正**：旧代码 `if not industry and not live:` 导致 `live=1` 时跳过行业获取，
>    库内行业/产业链事件在实时检索下 100% 查不到。现在固定为 画像 → 行业（参数/东财/F10）
>    → 库查询 → 实时检索；东财板块行业与 F10 行业链末级**两套分类都查**并按标题去重。
> 3. **关键词来自真实经营范围且可回溯**：`stock_profile._terms_with_evidence` 按连接词
>    （以及/及其/等/包括）切开，剥括号套话、"…的…"取主词，每个词附 `keyword_evidence`
>    （出处于哪一小句）。不扩同义词，匹配不到的宁可不给。
> 4. **检索状态四档**：`success / empty / partial / failed`，两个源分别记状态；
>    数据源超时不再显示成"没有相关政策"。
> 5. **数据健康面板**：`/api/data-health` + 首页底部卡片，十项状态
>    （REAL/CACHED/STALE/PARTIAL/UNAVAILABLE），只读缓存与 SQLite，零外部请求。
> 6. **估值透明化**：行情时间/来源/是否过期、行业 PE 来源与静态标记、财报期、原始 EPS、
>    年化系数、安全边际、偏离口径全部随响应返回并在页面显示；历史估值分位**未实现**，
>    页面明写"未接入"。
> 7. **打包扫描器修掉转义绕过**：`"api\u005fkey"` 这类 JSON 转义字段名，在小文件里因为
>    走 `json.loads` 能查到，但 >2MB 走字节状态机时反斜杠被丢掉 → 命中 0。
>    已修（状态机保留转义符 + `_unescape_json` 还原后再匹配），实测 2.2MB 文件命中 1。
> 仍希望被挑刺的点见第 5 节。

---

## 1. 30 秒跑起来（不需要任何 API key）

```bash
python3 -m unittest discover -s tests -v      # 320 个用例，纯离线，不联网
python3 app.py                                # 打开 http://127.0.0.1:5000
```

说明：

- 发布包**不含 `config_local.py`**（里面是 DeepSeek key，故意排除）。AI 分析接口会返回
  "未配置 key"，其它页面（行情/资金流/估值/政策事件）都能正常跑。
- 数据来自腾讯行情、东方财富公开接口，需要能访问外网；接口失败时页面显示"暂无数据"，
  **不会退化成随机数**（见第 3 节第一条）。
- `data/events.db` 也不在包里（本地运行时数据，首次启动自动建表）。想灌入示例数据：
  `python3 scripts/events_cli.py seed`（内置 4 条行业事件）。
- **没有 `/events` 页面**：政策事件在 `估值偏离 → 个股估值 → 政策与事件` 内按个股展示。

---

## 2. 目录地图

```
app.py                  Flask 入口 create_app() + 统一鉴权守卫（DEBUG 时强制绑 127.0.0.1）
routes.py               25 条路由；API 统一返回 {"code":200,"data":...}
data_service.py         数据层：腾讯/东财取数、缓存、降级、口径判断、数据源统计（最厚的一块）
chart_builder.py        把数据转成 ECharts option（纯函数，好测）
stock_profile.py        个股画像：东财 F10 → 行业链/经营范围/关键词+证据，6h 缓存
policy_live.py          实时政策检索：公告 + 快讯，四档归因，success/empty/partial/failed
policy_notes.py         老的硬编码事件字典，现在只作 fallback + seed 源
events_store.py         SQLite 事件库：events / event_links / feed_seen + 关联查询 + AI 抽取
event_feed.py           快讯抓取（东财/新浪）+ 关键词分级 + 近似去重 + seen 去重
data_health.py          数据健康快照：只读缓存与 DB，不发外部请求
market_calendar.py      交易日历（上交所休市安排）
technical.py            EMA/ZigZag/支撑压力/趋势线/斐波那契
ai_analysis.py          DeepSeek 分析（可选）
scripts/events_cli.py   事件录入 CLI：seed/add/add-text/feed/link/archive/repair
scripts/package_release.py  打发布包（排除密钥/缓存/本地库，入包出包各扫一次密钥）
scripts/feed_daily.sh   每日抓取（crontab 调，只读不写库）
templates/, static/     页面与前端；static/vendor/echarts.min.js 是 vendor 的 ECharts
tests/                  320 个离线单测（数据层/缓存 SWR/认证/扫描器/政策口径/API/前端契约）
```

---

## 3. 三条不变量（评审时可以先按这个尺子量）

1. **能算就说能算，不能算就说不能算。** PE 缺失不再用行业均值顶替、EPS 缺失不再用
   `price/pe` 反推、随机数兜底默认关闭（`ALLOW_MOCK_FALLBACK=False`）。宁可显示
   "数据不可用"，也不给一个看似合理的假结论。
2. **AI 只出初稿，人按确认键。** `events_cli feed --pick` 抽完先打印，`--yes` 才写库；
   提示词里明确写"原文没有的数字一律不要编"；实时检索结果一律 `review_status=pending` +
   `direction=待确认`，不进任何结论。
3. **取不到 ≠ 没有。** 检索状态分 `success/empty/partial/failed`；数据健康里没抓取时间的
   缓存直接标 `STALE` 并写"时间未知"，不把缓存说成实时。

---

## 4. 我已知的取舍（不是没想到，是这版故意没做）

| 取舍 | 原因 | 代价 |
|---|---|---|
| 不做全自动入库 | 政策事件写错比没有更糟，模型会把标题党写成政策 | 每天要人花 2 分钟 `--pick --yes` |
| 不做事件对股价的量化（事件研究/超额收益） | 库里只有 4 条事件，样本量根本不够跑统计 | "政策→股价"目前只是定性提示 |
| 事件库是 SQLite 单文件 | 单机自用，并发写极少 | 多进程写会 `database is locked` |
| ECharts vendor 成 1MB 本地文件 | jsdelivr 在本机被墙，导致整页图表空白 | 发布包变大 |
| 鉴权只做 HTTP Basic + 仅回环免认证 | 单机自用，不想引入用户体系 | Basic 必须套 HTTPS 反代，不能直接暴露公网 |
| 缓存是 JSON 文件不是 Redis | 单机、零依赖 | 无 TTL 淘汰策略，只有按 key 覆盖 |
| 前端原生 JS、无构建 | 改一行刷新就生效 | 没有组件化，`app.js` / `valuation.js` 偏长 |
| 历史估值分位没做 | 需要长期历史 PE 序列，现有数据源只有当下 | 估值页明确写"未接入"，不给假分位 |

---

## 5. 重点想请你审的地方

1. **数据真实性**：`data_service.py` 里还有没有"静默兜底"没关干净？特别是
   `get_sector_flow` / `get_index_history` / `get_sector_valuation` 三条链路，
   异常时会不会返回看似正常的旧数据或空壳？
2. **口径正确性**：`get_stock_valuation` 的 PE 偏离、`estimate_intrinsic_value`
   的 `EPS × 行业PE × VALUATION_SAFETY_MARGIN`，数学上有没有硬伤？
   （0.8 已改成 `config.VALUATION_SAFETY_MARGIN`，接口在 `model_params` 里回显
   `report_date/raw_eps/annual_factor`；没有财报 EPS 时不再用 price/PE 反推值参与计算，
   只单独给一个标注"非财报口径"的相对 PE 参考值。想听你怎么看这个系数该不该暴露成参数。）
3. **SQL 与输入校验**：`events_store.py` 里所有 SQL 都是参数化的，但
   `list_events` 的 `where` 是字符串拼接（只拼固定片段，参数仍走 `?`）。
   请确认没有注入面；`/api/policy-events` 的 `stock_code/industry/live/refresh` 校验够不够。
4. **抓取健壮性**：`event_feed.py` 依赖东财 JSONP（`var ajaxResult={...}`）和
   新浪 JSON 的字段名，接口一改就静默少数据 —— 现在至少会体现为 `partial/failed`
   而不是 `empty`，但**怎么主动发现字段改名**？除了单测里的固定报文，还该怎么监控？
5. **去重策略**：`SequenceMatcher` 阈值 0.85 是试出来的，会不会误合并两条不同政策？
   指纹只用 `日期|标题`，同一事件不同来源标题不同就会重复入库，有没有更好的方案？
   个股政策去重目前只按"去空白后的标题"完全相等，也请评估。
6. **打包安全**：`scripts/package_release.py` 用"目录黑名单 + 文件名前缀/后缀 + 字节级
   JSON 状态机 + 正则扫密钥"。本轮修掉了转义字段名绕过（`"api\u005fkey"`），
   并验证了：转义键、嵌套键、敏感键下的数组（600 项全报）、跨分块边界、>2MB 文件。
   黑名单永远是补出来的 —— 请继续找洞（二进制里藏 key、zip 路径穿越、Windows 保留名、
   大小写/Unicode 归一化绕过、YAML/TOML 里的引号变体）。
7. **前端健壮性**：`valuation.js` 的图表渲染有 try/catch，切换股票用"请求序号 + AbortController"
   双保险；但 `app.js`（总览/热力/数据健康）没有这层保护，ECharts 挂掉整页就空。
   要不要抽公共壳？
8. **数据健康的口径**：`STALE` 的阈值是"15 分钟 REAL / 1 天 CACHED / 超过 1 天 STALE 或时间未知"，
   有没有更合理的分档？`外部数据源成功率`是进程内计数（重启清零），你是否认为必须落到磁盘。
9. **测试覆盖的边界**：320 个用例覆盖 routes 的超时/4xx/5xx/非法 JSON/空数据/缺字段/
   旧缓存兜底/stale/失败不覆盖缓存，认证 fail-closed，扫描器转义与跨块，
   前端 JS 用 node 实跑（`format.js` 数值格式、`valuation.js` 政策竞态）。
   但仍是**基于 mock 的契约测试**：真实数据源改字段名、真实 DeepSeek 返回结构变化，
   现有 mock 不会自动发现。你会怎么补这一层（录制回放 / 真实 schema 校验 / 定时巡检）？

---

## 6. 验证命令（都是真跑过的，不是写在文档里的愿望）

```bash
python3 -m unittest discover -s tests          # 320 tests, OK
python3 scripts/events_cli.py stats            # 事件 4 / 关联 15 / 孤儿 0
python3 scripts/events_cli.py list --code 000657   # 3 条钨相关事件
python3 scripts/events_cli.py feed --strong    # 抓快讯，只留强相关候选
python3 scripts/package_release.py --dry-run   # 扫描但不写产物
python3 scripts/package_release.py             # 打包含密钥扫描
```

页面自检（需要能开浏览器）：

- `/valuation` → 选个股 → 「政策与事件」自动检索，状态行显示 success/empty/partial/failed
- `/`（首页）底部 → 「数据健康」表：每行状态 + 更新时间 + 来源
- 快速切三只股票，政策区不会出现上一只的结果

---

## 7. 版本与已知未完成

- 分支 `main`；本轮基线版本 V2.0.2（发布包不含 `.git`）
- 数据口径文档：`README.md` 的「数据可信度」一节，逐条列了每根曲线的来源/刷新/失败行为
- **已知未完成**（不要当成已实现）：
  - 事件影响量化（事件研究/超额收益）—— 未做
  - 历史估值分位 —— 未做，页面明示不可用
  - 政策原文全文解析 —— 只有标题 + 摘要，机制判断据此写成"资料不完整"
  - AI 分析历史记录持久化 —— 未做，数据健康面板只能显示"已配置，无记录"
  - `data_service.py` 拆层、API 契约测试 —— 未做

---

## 8. 你可以直接这样问我

> "第 5 节第 2 条，0.8 这个折价系数没有依据，应该改成参数并默认 1.0。"
> "第 6 节能攻：把 `config_local.py` 改成 `config_local.py.txt` 照样绕过黑名单吗？"
> "第 8 条，进程内成功率重启就没了，等于没有监控。"

这几种回复我都欢迎——我要的是能落地的批评，不是"整体不错"。
