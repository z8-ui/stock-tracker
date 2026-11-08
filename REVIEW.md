# 代码评审向导（REVIEW）

这份文件是给外部评审者（人或 AI）看的：说明这个项目想做什么、哪些地方是刻意取舍、
哪些地方我知道还没做好，以及**我希望你重点挑什么毛病**。

> 项目：A 股个股/板块数据看板 + 估值偏离 + 个股政策事件（本地 SQLite）
> 形态：单进程 Flask + 原生 JS + ECharts + SQLite，纯标准库单测，无前端构建
> 规模：约 6.0k 行 Python（不含测试）/ 1.6k 行 JS，**456 个离线单测**（历史：V2.0.2 为 409，S1 收尾后 456）

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
python3 -m unittest discover -s tests -v      # 456 个用例，纯离线，不联网（需 flask/requests）
python3 app.py                                # 打开 http://127.0.0.1:5000
```

说明：

- 发布包**不含 `config_local.py`**（里面是 DeepSeek key，故意排除）。AI 分析接口会返回
  "未配置 key"，其它页面（行情/资金流/估值/政策事件）都能正常跑。
- 数据来自腾讯行情、东方财富公开接口，需要能访问外网；接口失败时页面显示"暂无数据"，
  **不会退化成随机数**（见第 3 节第一条）。
- `data/events.db` 也不在包里（本地运行时数据，首次启动自动建表）。想灌入示例数据：
  `python3 scripts/events_cli.py seed`（内置静态策池 15 条文案 → 去重后 4 条事件 / 15 条关联）。
  seed 是**幂等 + 单事务**的：重复跑不会产生重复记录，不覆盖库里已有事件与人工审核状态，
  中途失败整体回滚、不留半截数据（细节见第 7 节债务 1）。
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
tests/                  456 个离线单测（数据层/缓存 SWR/认证/扫描器/政策口径/API/前端契约/seed 事务/Provider/夹具）
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
9. **测试覆盖的边界**：456 个用例覆盖 routes 的超时/4xx/5xx/非法 JSON/空数据/缺字段/
   旧缓存兜底/stale/失败不覆盖缓存，认证 fail-closed，扫描器转义与跨块，
   前端 JS 用 node 实跑（`format.js` 数值格式、`valuation.js` 政策竞态）。
   但仍是**基于 mock 的契约测试**：真实数据源改字段名、真实 DeepSeek 返回结构变化，
   现有 mock 不会自动发现。你会怎么补这一层（录制回放 / 真实 schema 校验 / 定时巡检）？

---

## 6. 验证命令（都是真跑过的，不是写在文档里的愿望）

```bash
python3 -m unittest discover -s tests          # 456 tests, OK
python3 scripts/events_cli.py stats            # 本机：事件 4 / 关联 16 / 孤儿 0（新库 seed 后是 4 / 15）
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

## 7. 版本与技术债务台账

- 分支 `main`；基线版本 **V2.0.2**（`8cebf99`，发布包不含 `.git`）。本轮只做
  **seed 事务修复 + 文档**，不新增业务功能、不改架构。
- 数据口径文档：`README.md` 的「数据可信度」一节，逐条列了每根曲线的来源/刷新/失败行为。
- 状态只有四档，**不许把计划写成已完成**：
  `已实现` / `部分实现` / `未实现` / `计划开发`（阶段编号对应 `docs/V2.1_ROADMAP.md`）。

| # | 事项 | 状态 | 依据 / 缺口 |
|---|---|---|---|
| 1 | 政策事件 seed 初始化 | 已实现 | 见 7.1 |
| 2 | seed 幂等、不覆盖既有事件与人工审核状态 | 已实现 | 见 7.1，`tests/test_events_store.py::TestSeed` |
| 3 | seed 事务原子性（失败整体回滚） | 已实现（2026-11 修复） | 见 7.2 |
| 4 | 历史 PE / PB 估值分位 | 未实现 | 见 7.3；规划 D2 |
| 5 | 政策全文分析 | 部分实现 | 见 7.4；只有标题+摘要+关键词检索，原文与传导分析未做 |
| 6 | AI 分析记录持久化 | 未实现 | 见 7.5；规划 D4 |
| 7 | 数据源成功率持久化 | 部分实现（进程内） | 见 7.6；重启清零，规划 D5 |
| 8 | `data_service.py` 拆层、Provider 标准化 | 未实现 | 规划 D1 |
| 9 | 事件影响量化（事件研究/超额收益） | 未实现 | 库里只有 4 条事件，样本量不够，暂不做 |
| 10 | 真实浏览器点击复测 | 未测试（NOT TESTED） | 现有验证是 Flask test_client + node 跑前端 JS |

### 7.1 债务 1：本机事件库不入库，新用户靠 seed 初始化

- 本机 `data/events.db` 里有 **4 条政策事件 / 16 条关联**（钨产业链）。其中 15 条关联是 seed
  自带的，另有 1 条是手工挂在 `2026-06-10 钨原料长单采购价` 那条上的 `industry:小金属`；
  全新库跑 seed 得到的是 4 事件 / 15 关联。属于运行时数据：
  `.gitignore` 排除 `data/`，**不进 GitHub、不进发布包**。这是刻意设计 —— 事件是人工维护的
  本机资产，跟着仓库走会变成"永远追不上的 diff"。
- 新用户 clone 后政策区为空，需要一次性初始化：

  ```bash
  python3 scripts/events_cli.py seed      # 从 policy_notes.py 静态策池导入
  python3 scripts/events_cli.py stats     # 期望 事件 4 / 关联 15 / 孤儿 0
  ```

- **安全约束（双向都成立）**：
  1. 数据源只有 `policy_notes.py` 的静态字典（个股 4 只 / 12 条 + 行业 1 个 / 3 条，
     去重后 4 条事件），**不读本机 `data/events.db`**，不含个人信息、API key、本机路径；
  2. 幂等：指纹 `sha1(日期|标题)`，重复执行 `created=0`，不会产生重复行；
  3. 命中已有事件时只补空的 `occurred_at/published_at` 与缺失标的，
     **不改标题/摘要/方向/来源/审核状态**（人工改过的内容保得住）；
  4. 新建的静态条目带 `review_status='approved'`（静态池本身是人工维护的），
     但**不会把库里 pending 的事件升成 approved**；
  5. 只增不删：seed 不会删除任何已有事件。

### 7.2 债务 3：seed 事务原子性 + 嵌套事务安全（本轮修复）

- 修复前（两个断点，不是一个）：`seed_from_static()` 逐条调 `add_event()`，而 `add_event()`
  每条末尾自己 `conn.commit()`；`add_event()` 还会每次调 `init_db()`，而 `init_db()` 用
  `executescript()` —— sqlite3 在执行 `executescript` 前会**隐式 COMMIT**。
  结果：一次 seed 被拆成 N 个小事务，中途失败留半截数据。
  实测（HEAD `8cebf99`，第 3 条写入抛 `OperationalError`）：**残留 3 条事件 / 3 条关联**。
- 修复后：
  - `events_store.transaction(conn)`：**纯 SAVEPOINT 语义，不碰调用方事务**。
    成功只 `RELEASE` 本层（本层即最外层时 SQLite 规定等于提交）；失败只
    `ROLLBACK TO SAVEPOINT + RELEASE` 再把异常抛出 —— **不再无条件 `commit()` / `rollback()`**；
  - `add_event(..., commit=True)` 新增形参：批量写入传 `commit=False`，只发语句不提交；
    `add_event(conn=None, commit=False)` 在建连接**之前**直接 `ValueError`（危险组合，关闭时会丢写入）；
  - `_ensure_schema(conn, own)`：结构已就绪就不迁移；结构不完整且**连接已在事务中**时明确拒绝
    （`RuntimeError`，说明 `executescript` 会隐式提交），只有无外层事务时才 `init_db()`；
    迁移后再复核一次，仍不合格（例如缺唯一约束，`init_db` 补不了）直接报"无法自动迁移"，
    绝不留在半成品结构上继续写；
  - `_schema_ready(conn)` 的判定范围（2026-11 二次 + 三次加固）：`events` 与 `event_links` 两张表都在、
    `events` 17 个写入字段齐全（含 `published_at` / `collected_at`）、`event_links` 含 `relation_type`、
    `events.fingerprint` 与 `event_links(event_id,target_type,target_key)` 的**完整**唯一约束在
    （`partial` 唯一索引不算：只约束部分行，重复数据会漏进来）、
    两个普通索引比对**真实结构**而非名字 —— `idx_events_date` 必须属于 `events` 且列是
    `event_date DESC`，`idx_links_target` 必须属于 `event_links` 且列顺序是
    `target_type, target_key`（用 `PRAGMA index_xinfo` 读列与 `desc`，`PRAGMA index_list` 读 `partial`）。
    旧版只查 events 的 8 个列，对"events 齐全但 event_links 不存在"的库误判就绪 →
    seed 在事务里撞 `no such table: event_links`（实测旧判定报这个错，新判定自动补建后 4 事件 / 15 关联）；
    上一版只查索引名字，对"名字对但建在 `events.title` / `event_links.event_id` 上"的库也误判就绪
    （实测旧名字判定 True、结构判定 False），这类库排序与过滤都是错的，必须重建成正确结构。
    索引同名但结构错误时 `init_db` 补不了（`CREATE INDEX IF NOT EXISTS` 会跳过），
    此时明确报"无法自动迁移"，要求人工处理而不是静默接受；
  - `init_db(conn)` 明确事务契约：传入的 conn 已在事务中 → 直接 `RuntimeError`，不执行
    `executescript`（它不是干净的初始化入口时就不许动）；自己新建连接的初始化路径不变。
    `stats()` / `timeline()` 也从无条件 `init_db` 改为 `_ensure_schema` —— 只读统计不该顺手提交别人的事务；
  - `seed_from_static()` 用一个 `with transaction(conn)` 包住全部写入，`finally` 关闭自建连接，
    异常原样抛给调用方（CLI 打印明确失败原因并 `exit 1`）。
- 同场景复测：**残留 0 条事件 / 0 条关联**，随后正常 seed 完整初始化 4 条。
- 嵌套事务实测（上一版 `transaction` 对照）：外层事务里跑 seed 成功 → 旧实现让外层
  `in_transaction=False`、第二个连接已能看到 5 条（外层被提交）；失败 → 旧实现把外层标记
  也滚掉了。修复后两种情况外层事务都完好，提交/回滚由调用方决定。
- 覆盖测试（`tests/test_events_store.py`，共 45 个用例，本轮累计新增 30 个）：
  A 全新库 / B 重复执行 / C 已有事件保护（含"同指纹不覆盖"）/ D 审核状态保护（双向）/
  E 中途失败整体回滚（**用全新连接读文件校验**）/ F 外层事务中 seed 成功不提交外层 /
  G 外层事务中 seed 失败只回滚自己那层（外层仍可提交、也可回滚）/ H 拒绝 `conn=None + commit=False` /
  I 结构不完整且连接在事务中 → 拒绝迁移且不隐式提交 + 旧库在事务外仍能迁移 /
  J schema 契约（`TestSchemaContract` 15 个）：events 齐全但 event_links 缺失、
  event_links 缺 relation_type、events 缺 published_at/collected_at、缺索引、
  缺唯一约束必须报"无法自动迁移"、**索引名字对但列错 / 表错 / 列顺序错 / 少 DESC /
  partial 唯一索引不算数**、`init_db(conn)` 在事务中拒绝且外层仍可提交/回滚、自建连接初始化幂等。
  全量：**456 tests OK**（V2.0.2 基线为 409，属历史数量）。

### 7.3 债务 4：历史 PE / PB 估值分位 —— 未实现

现在只有"当前 PE 对比行业 PE"和 `EPS × 行业PE × 安全边际` 的内在价值估算，
**没有任何历史分位**：缺 3 年 / 5 年历史 PE、PB 序列（现用数据源只给当下快照）。
估值页明确写"未接入"，不给假分位。方案见 `docs/V2.1_ROADMAP.md` D2。

### 7.4 债务 5：政策全文分析 —— 部分实现

已实现：事件标题 / 摘要 / 方向 / 来源 / 关联标的、静态策池、快讯半自动采集（人工 `--pick --yes`）、
个股画像关键词 + 出处（`keyword_evidence`）、两个数据库检索源与四档状态（success/empty/partial/failed）。
**未实现**：官方政策原文抓取、条文要求识别、主营业务→收入/成本/利润的传导分析、证据链留存。
所以机制字段现在只能是"资料不完整"级别，不出结论。方案见 D3。

### 7.5 债务 6：AI 分析记录持久化 —— 未实现

现在 DeepSeek 分析结果只落在 `ai_cache.json`（文本缓存），**没有结构化分析记录**：
没有股票代码 / 分析时间 / 模型版本 / 所用数据日期 / 数据快照版本 / 证据引用的历史库，
无法按股票复盘"上次为什么这么判"。数据健康面板只能显示"已配置，无记录"。方案见 D4。

### 7.6 债务 7：数据源成功率 —— 进程内统计，重启清零

`data_service` 的成功率/耗时/最近错误是模块级内存计数，**进程重启归零**，
所以健康面板的"历史成功率"实际只覆盖本次运行。要做成真正的监控需要落库（请求数/成功数/
失败数/响应时间/最后成功时间/最近错误），方案见 D5。

### 7.7 债务 8：本机半成品虚拟环境（非项目资产）

`/mnt/d/venvs/stock-v202` 是早前在 DrvFs 上装包超时留下的**半成品 venv**，
不属于项目源码、不在仓库、不随发布包外发。**本轮不删除、不移动、不清理**；
确认过的测试环境是 `/home/lenovo/.hermes/cache/scratch/venv-v202`（flask 3.1.3 / requests 2.34.2）。
如需删除 `/mnt/d/venvs/stock-v202`，须先取得用户明确同意。

### 7.8 V2.1 进展：S0 遗留清理 + S1 数据源 Provider 试点（2026-11-08）

- **S0（已完成）**：`.gitignore` 补 `.env` / `.env.*`（保留 `!.env.example`）；
  `/market-flow` 上证指数后面的 `undefined` 修掉 —— 根因是 `build_market_flow_chart()`
  只把交易日拼进 `title`、没放进 `data.date`；同时 `get_market_flow()` 不再用
  `datetime.now()` 兜底日期，`format.js` 新增 `fmt.date8()`（非法值返回空串，页面整段不显示）。
- **S1（试点完成）**：新增 `services/data/`（`base` / `exceptions` / `normalizers` / `registry` /
  `providers/tencent.py`），只搬了**腾讯行情快照**一个数据源；`data_service._tencent_quote`
  改为薄委托，函数名与返回结构不变，失败仍返回 `None`。
  盘点与设计见 `docs/V2.1_S1_ARCHITECTURE.md`（含 38 个函数的职责分类、数据源依赖图、
  7 处绕过统一传输的问题清单、S2 建议）。
- **顺带修的假数据**：`get_market_flow()` 全源失败时不再返回全零 dict（页面会渲染成
  `0.00 / 0.00 亿`，等于把失败伪装成真实零值），改为返回 `None` 走"暂无数据"。
- **S1 收尾（2026-11-08 晚）**：休市与故障分开 —— 新增 `data_health._session/_last_trading_day/_entry_status`，
  周末展示最近交易日数据标 CACHED 而不是 STALE（异常从 5/9 降到 2/9）；`get_market_flow()` 区分
  "源给了全 0 记录"（真实零值，保留）与"没拿到资金记录"（返回 None，不造零值）；
  新增 `services/data/telemetry.py` + `cache_stats()`，Provider 维度与缓存键维度分开统计，
  缓存命中不再冒充上游请求；文档统计口径已按实际代码修正（1388 行 / 41 函数 / B 类 12 个）。
- **S2 前置 hotfix（2026-11-08 晚）**：真实报文核对发现 `get_market_flow()` 把 f69/f75（占比）
  当"中单/小单"金额、且 `val2yi()` 按数值大小猜单位 —— 页面上的中单/小单其实是百分比。
  已改为 f78/f84 + `yuan_to_yi()`（一律 元→亿），缺失不填 0；旧缓存用 `_flow_fields_v=2` 自愈重取。
  真实报文回放：中单 -279.00→45.83 亿、小单 -348.00→479.15 亿，守恒残差 -1151.97→0.01。
  夹具见 `tests/fixtures/eastmoney/`，回归 `tests/test_market_flow_fields.py`（14 例）。
- **仍然没有的**：东财系列 Provider（资金流 / 板块 / F10 / 行业 PE 还在 `data_service` 里）、
  缓存层独立、Routes 分层 —— 都按规划逐轮搬，一次一类。


---

## 8. 你可以直接这样问我

> "第 5 节第 2 条，0.8 这个折价系数没有依据，应该改成参数并默认 1.0。"
> "第 6 节能攻：把 `config_local.py` 改成 `config_local.py.txt` 照样绕过黑名单吗？"
> "第 8 条，进程内成功率重启就没了，等于没有监控。"

这几种回复我都欢迎——我要的是能落地的批评，不是"整体不错"。
