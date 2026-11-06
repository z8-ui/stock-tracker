# 代码评审向导（REVIEW）

这份文件是给外部评审者（人或 AI）看的：说明这个项目想做什么、哪些地方是刻意取舍、
哪些地方我知道还没做好，以及**我希望你重点挑什么毛病**。

> 项目：A 股个股/板块数据看板 + 估值偏离 + 政策事件中心
> 形态：单进程 Flask + 原生 JS + ECharts + SQLite，纯标准库单测，无前端构建
> 规模：约 5.5k 行 Python / 1.3k 行 JS，54 个离线单测

---

## 1. 30 秒跑起来（不需要任何 API key）

```bash
python3 -m unittest discover -s tests -v      # 54 个用例，纯离线，不联网
python3 app.py                                # 打开 http://127.0.0.1:5000
```

说明：

- 发布包**不含 `config_local.py`**（里面是 DeepSeek key，故意排除）。因此 AI 分析接口
  会返回“未配置 key”，其它页面（行情/资金流/估值/事件）都能正常跑。
- 数据来自腾讯行情、东方财富公开接口，需要能访问外网；接口失败时页面显示“暂无数据”，
  **不会退化成随机数**（见第 4 节第一条）。
- `data/events.db` 也不在包里（本地运行时数据）。首次运行事件页是空的，可以：
  `python3 scripts/events_cli.py seed` 把内置的 4 条行业事件导进去。

---

## 2. 目录地图

```
app.py                  Flask 入口；DEBUG 时强制绑 127.0.0.1（防 Werkzeug 调试器暴露）
routes.py              25 条路由；API 统一返回 {"code":200,"data":...}
data_service.py        数据层：腾讯/东财取数、缓存、降级、口径判断（最厚的一块）
chart_builder.py       把数据转成 ECharts option（纯函数，好测）
policy_notes.py        老的硬编码事件字典，现在只作 fallback + seed 源
events_store.py        政策事件中心：SQLite 两表 + 时间线聚合 + AI 抽取
event_feed.py          快讯抓取（东财/新浪）+ 关键词分级 + 近似去重 + seen 去重
scripts/events_cli.py  事件录入 CLI：seed/add/add-text/feed/link/archive/repair
scripts/package_release.py  打发布包（排除密钥/缓存/本地库，入包出包各扫一次密钥）
scripts/feed_daily.sh  每日抓取（crontab 调，只读不写库）
templates/, static/    页面与前端；static/vendor/echarts.min.js 是 vendor 的 ECharts
tests/                 54 个离线单测（数据层纯函数、图表、事件库、快讯解析）
```

---

## 3. 三条不变量（评审时可以先按这个尺子量）

1. **能算就说能算，不能算就说不能算。** PE 缺失不再用行业均值顶替、EPS 缺失不再用
   `price/pe` 反推、随机数兜底默认关闭（`ALLOW_MOCK_FALLBACK=False`）。宁可显示
   “数据不可用”，也不给一个看似合理的假结论。
2. **AI 只出初稿，人按确认键。** `events_cli feed --pick` 抽完先打印，`--yes` 才写库；
   提示词里明确写“原文没有的数字一律不要编”。
3. **一条事件只存一份文案。** `events` + `event_links(target_type=stock|industry)`，
   一条“禁止废钨出口”挂 4 只钨股 + 1 个行业，改一次全站生效；去重靠
   `sha1(日期|标题)` 指纹。

---

## 4. 我已知的取舍（不是没想到，是这版故意没做）

| 取舍 | 原因 | 代价 |
|---|---|---|
| 不做全自动入库 | 政策事件写错比没有更糟，模型会把标题党写成政策 | 每天要人花 2 分钟 `--pick --yes` |
| 不做事件对股价的量化（事件研究/超额收益） | 库里只有 4 条事件，样本量根本不够跑统计 | “政策→股价”目前只是定性提示 |
| 事件库是 SQLite 单文件 | 单机自用，并发写极少 | 多进程写会 `database is locked` |
| ECharts vendor 成 1MB 本地文件 | jsdelivr 在本机被墙，导致整页图表空白 | 发布包 90KB → 420KB |
| 无登录/无鉴权 | 只监听回环、本机自用 | 不能直接暴露公网（README 有写） |
| 缓存是 JSON 文件不是 Redis | 单机、零依赖 | 无 TTL 淘汰策略，只有按 key 覆盖 |
| 前端原生 JS、无构建 | 改一行刷新就生效 | 没有组件化，`app.js` 偏长 |

---

## 5. 重点想请你审的地方

1. **数据真实性**：`data_service.py` 里还有没有“静默兜底”没关干净？特别是
   `get_sector_flow` / `get_index_history` / `get_sector_valuation` 三条链路，
   异常时会不会返回看似正常的旧数据或空壳？
2. **口径正确性**：`get_stock_valuation` 的 PE 偏离、`estimate_intrinsic_value`
   的 `EPS × 行业PE × 0.8`，数学上有没有硬伤？（我知道 0.8 是拍脑袋的折价系数，
   想听你怎么看这个系数该不该暴露成参数。）
3. **SQL 与输入校验**：`events_store.py` 里所有 SQL 都是参数化的，但
   `list_events` 的 `where` 是字符串拼接（只拼固定片段，参数仍走 `?`）。
   请确认没有注入面；`/api/event-timeline` 的 `days/month/industry` 校验够不够。
4. **抓取健壮性**：`event_feed.py` 依赖东财 JSONP（`var ajaxResult={...}`）和
   新浪 JSON 的字段名，接口一改就静默少数据。除了单测里的固定报文，还该怎么监控？
5. **去重策略**：`SequenceMatcher` 阈值 0.85 是试出来的，会不会误合并两条不同政策？
   指纹只用 `日期|标题`，同一事件不同来源标题不同就会重复入库，有没有更好的方案？
6. **打包安全**：`scripts/package_release.py` 用“目录黑名单 + 文件名前缀/后缀 + 正则扫密钥”。
   我自测过三种诱饵：`.env.decoy` 和 `config_local_backup.txt` 被前缀规则剪掉，
   `notes_fake.txt` 里的 `api_key = ...` 会让打包直接中止（exit 1，不写文件）。
   但黑名单永远是补出来的——请找剩下的洞（比如二进制文件里藏 key、符号链接、
   `MAX_SCAN_BYTES = 8MB` 的超大文件跳过扫描、内网路径泄漏）。
7. **前端健壮性**：`static/js/events.js` 里图表渲染包了 try/catch，表格不受影响；
   其它页面（`app.js`）还没有这层保护，ECharts 挂掉整页就空。要不要抽公共壳？
8. **测试盲区**：54 个用例全是纯函数/离线，`routes.py` 和 `data_service.py` 的
   网络分支**一行都没覆盖**。以你的经验，这层最该先补哪种测试（契约测试？录制回放？）

---

## 6. 验证命令（都是真跑过的，不是写在文档里的愿望）

```bash
python3 -m unittest discover -s tests          # 54 tests, OK
python3 scripts/events_cli.py stats            # 事件 4 / 关联 15 / 孤儿 0
python3 scripts/events_cli.py list --code 000657   # 3 条钨相关事件
python3 scripts/events_cli.py feed --strong    # 抓 100 条快讯，只留强相关候选
python3 scripts/package_release.py             # 打包含密钥扫描
```

事件页（需要能开浏览器）：`/events` → 点柱子筛该月、再点取消、点 × 清空。

---

## 7. 版本

- 分支 `main`，最近提交见 `git log --oneline -8`（发布包不含 `.git`）
- 数据口径文档：`README.md` 的「数据可信度」一节，逐条列了每根曲线的来源/刷新/失败行为
- 已知未完成：事件影响量化（V2.0 P3）、`data_service` 拆层、API 契约测试

---

## 8. 你可以直接这样问我

> “第 5 节第 2 条，0.8 这个折价系数没有依据，应该改成参数并默认 1.0。”
> “第 6 条能攻：把 `config_local.py` 改成 `config_local.py.txt` 就能绕过黑名单。”

这两种回复我都欢迎——我要的是能落地的批评，不是“整体不错”。
