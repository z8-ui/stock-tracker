# S3-0 真实报文夹具（2026-11-10 采集）

8 份**真实上游响应**（不是 mock、不是构造样本）：6 类接口，其中行业 PE 是三步链，分别存 3 份。
采于 2026-11-10 20:44–20:45 CST（收盘后），采集环境见 `manifest.json.capture_env`。

用途：给 S3-1…S3-6 的迁移提供**可离线回放的差分基线**。
本目录只放证据，不放解析结果。

## 1. 目录结构

| 文件 | 含义 |
|---|---|
| `<key>.raw` | 响应**原始字节**，直写落盘。不格式化、不裁剪字段、不做单位换算 |
| `<key>.meta.json` | 采集元数据：URL、完整参数、HTTP 状态、Content-Type、字节数、sha256、请求时间、依赖关系、数据日期、失败原因 |
| `manifest.json` | 8 份汇总 + sha256 清单 + 采集环境（host/python/requests/git_head） |
| `field_spec.json` | 字段/类型/单位/可空性规格；测试据此发现上游结构漂移 |
| `META_SCHEMA.md` | **元数据 13 项定义**（source/interface/url/params/method/requested_at/http_status/content_type/bytes/data_date/sha256/capture_env/outcome），文档/测试/JSON 三处口径以此为准 |
| `CAPTURE_RUN.log` | 两轮采集的原始输出（证明每个接口只请求 1 次） |
| `README.md` | 本文件 |

## 2. 八份清单

| key | 接口 | HTTP | 字节 | sha256(前16) | data_date | 形态 |
|---|---|---|---|---|---|---|
| `tencent_quote_sh600519` | qt.gtimg.cn 实时快照（个股行情） | 200 | 547 | `d2324d8c5d800f14` | 20261110 | GBK 文本，88 个 `~` 字段 |
| `em_sector_flow` | push2 clist 行业板块资金流（fs=m:90+t:2, fields=f14,f2,f3,f62） | 200 | 4016 | `2b841e069bf6d5bb` | — | JSON，60 行（total=496） |
| `em_sector_valuation` | push2 clist 行业板块估值（fields=f14,f2,f3,f9,f20,f23,f25,f37） | 200 | 6686 | `82ca265d441e0d3a` | — | JSON，60 行，f23/f37 全为 "-" |
| `em_f10_600519` | datacenter-web F10 主要财务指标（RPT_F10_FINANCE_MAINFINADATA） | 200 | 4299 | `5a5f3881febccdff` | 2026-09-30 | JSON，1 行 × 165 列 |
| `em_industry_pe_step1_stock` | 行业PE 第1步：个股 → 行业名（f57,f127） | 200 | 112 | `d2fcc423de6f7cac` | — | JSON，单对象 |
| `em_industry_pe_step2_suggest` | 行业PE 第2步：行业名 → 板块代码（suggest type=14） | 200 | 361 | `b0ce66528cb2e900` | — | JSON，1 行（BK1277） |
| `em_industry_pe_step3_ulist` | 行业PE 第3步：板块 → 动态PE（f9，放大100倍） | 200 | 143 | `38b67a24dc7bf724` | — | JSON，1 行（f9=2274） |
| `tencent_index_kline_sh000001` | web.ifzq fqkline 指数日K（sh000001, qfq） | 200 | 3942 | `5b1a913d07b7280c` | 2026-11-10 | JSON，30 根日 K |

> `data_date` 为 `—` 表示**该接口本身不返回数据日期**（不是漏填），只能记录 `fetched_at`。
> 采集时间不是数据日期，两者不得互相顶替。

## 3. 怎么复算（不需要联网、不需要 pip 装东西）

```bash
cd /mnt/d/stock-tracker

# 夹具结构与 sha256 校验（61 例，纯离线）
python3 -m unittest tests.test_s3_0_fixtures -v

# 逐份核对哈希（与 manifest.json 对照）
cd tests/fixtures/s3_0 && sha256sum *.raw && cd -
```

重新采集（**会真实发请求**，每接口 1 次；仅在确认要更新基线时执行）：

```bash
python3 scripts/capture_s3_0_fixtures.py            # 全量 8 个目标
python3 scripts/capture_s3_0_fixtures.py --dry-run  # 只看会请求哪些 URL
```

## 4. 字段与单位（详见 `field_spec.json`）

| 接口 | 关键点 |
|---|---|
| 腾讯行情 | `~` 分隔 GBK 文本，88 字段；索引 30 是 `YYYYMMDDHHMMSS`；索引 44 总市值单位**亿元**、57 成交额单位**万元** |
| 板块资金流 | `f62` 单位**元**（÷1e8 得亿元）；`f3` 单位 %；`total=496` 但只回 60 行 |
| 板块估值 | `f9` 为倍数（可为负）；`f20` 单位元；**`f23`/`f37` 本次 60/60 全是 `"-"`**（缺失） |
| F10 | `REPORT_DATE` 形如 `2026-09-30 00:00:00`；金额单位元；**56 个 `null` 与 2 个真实 `0.0` 并存** |
| 行业 PE 三步 | 第 3 步 `f9` 放大 100 倍（2274 → 22.74）；第 1 步 `f127` 是第 2 步入参；第 2 步 `BK1277` 是第 3 步入参 |
| 指数 K 线 | 6 列位置型数组；成交量是 `"404865177.000"` 字符串 → 必须 `int(float(x))`；`day` 外还有 `qt/prec/version/mx_price` |

## 5. 证据强度（别把弱证据当强证据）

- **真实网络报文**：8 份 `.raw` + 对应 `.meta.json`，sha256 冻结；
- **离线执行结果**：`tests/test_s3_0_fixtures.py` 61 例；
- **静态代码检查**：URL 漂移守卫（夹具 URL 的主机必须仍出现在被引用的源码文件里）；
- **尚未验证**：页面渲染（本轮不改前端，未做浏览器回归）；字段的**业务语义**只对
  代码实际读取的字段给出解释，未使用字段一律标 `unknown`，不猜测。

## 6. 已知限制

1. 采集时点是**收盘后**（腾讯快照时间 16:14:53），不代表盘中形态（盘中 `f3`/`f62` 会变）；
2. 板块 clist 与 F10 不返回数据日期，`data_date` 留 `null`；
3. 只采了 600519 一只票与上证指数一个标的，覆盖"结构"不覆盖"全市场分布"；
4. 北交所 `830799` 未采集（Q6=B，记为已知限制，不扩大 S3 范围）；
5. 上游若改字段名/列数，测试会失败——这是**设计意图**（结构漂移必须被看见），
   失败时重采并在 `fingerprint` 层面记录差异，不要直接把断言改宽。

## 7. 禁止事项

- 不要编辑、格式化、`json.dumps` 重排 `.raw`（哈希会变，证据失效）；
- 不要手改 `.meta.json` 的 `sha256`/`bytes`/`http_status`（造假）；
- 不要把 mock、人工构造、别的接口的报文放进本目录；
- 不要把 `fetched_at` 当 `data_date` 展示给用户。
