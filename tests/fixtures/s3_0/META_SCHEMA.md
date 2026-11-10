# S3-0 元数据字段定义（meta schema v1）

每份报文对应一个 `<key>.meta.json`。**13 项为必填**（任务书口径），其余为可选补充字段。
三者（本文件 / `tests/test_s3_0_fixtures.py` 的断言 / 实际 JSON）必须一致；
测试会逐字段核对本文件的字段名是否仍出现在每份 meta 里。

## 1. 必填 13 项

| # | 字段 | 类型 | 含义 | 取值约束 |
|---:|---|---|---|---|
| 1 | `source` | string | 数据源名称 | `tencent` / `eastmoney` |
| 2 | `interface` | string | 接口名称（人可读，含关键参数） | 非空，如 `push2 clist 行业板块资金流（fs=m:90+t:2, fields=f14,f2,f3,f62）` |
| 3 | `url` | string | 完整 URL（不含 query） | `http(s)://` 开头；参数在 `params` 里 |
| 4 | `params` | object | 请求参数（原样，不脱敏、不省略） | 可为空对象；派生参数必须与上游一致 |
| 5 | `method` | string | HTTP 方法 | 本轮全部 `GET` |
| 6 | `requested_at` | string | 请求时间（ISO 8601，带时区） | 如 `2026-11-10T20:44:45+08:00` |
| 7 | `http_status` | int \| null | HTTP 状态码 | 成功 200；未采集为 `null` |
| 8 | `content_type` | string | 响应头 Content-Type 原值 | 未采集为空串 |
| 9 | `bytes` | int | 响应体字节数 | 与磁盘文件大小一致 |
| 10 | `data_date` | string \| null | **数据所属日期** | `YYYY-MM-DD`；接口不返回则为 `null`（**必须是显式存在的键**） |
| 11 | `sha256` | string | 原始响应字节的摘要 | 64 位小写十六进制；`sha256sum` 可复算 |
| 12 | `capture_env` | object | 采集环境（内联副本） | 与 `manifest.json#capture_env` 完全相同；含 `host/platform/python/requests/repo/git_head/cwd` |
| 13 | `outcome` | string | 采集结果及异常说明（纯事实） | 成功：`采集成功：HTTP 200，547 字节，无异常`；失败：`未采集：<原因>` |

## 2. 关于 `data_date` 的硬规则

- 键**必须存在**：没有数据日期时值是 `null`，不能省略该键（省略会让"没采到"和"接口不提供"无法区分）；
- 不允许用 `requested_at`（抓取时间）顶替：板块 clist、F10、行业 PE 三个接口本身不返回数据日期；
- 腾讯行情的 `data_date` 由报文索引 30 的行情时间截取（`20261110` 形式），K 线取最后一根日期。

## 3. 关于 `capture_env` 的硬规则

- 每份 meta 内联一份完整副本，同时用 `capture_env_ref: "manifest.json#capture_env"` 指回汇总位置；
- 两边必须逐键相等（测试断言），避免"汇总里改了、单份没改"的口径漂移；
- 采集环境的 `git_head` 是**采集时点**的提交，早于 S3-0 提交本身，这是正确的时间顺序。

## 4. 可选补充字段（非必填，便于排查）

| 字段 | 含义 |
|---|---|
| `key` | 与文件名一致的标识 |
| `status` | `CAPTURED` / `NOT_CAPTURED` / `BLOCKED_HTTP_<code>` |
| `reason` | 仅在未采集时出现，写明原因 |
| `final_url` | 跟随重定向后的最终 URL（如 K 线 http→https） |
| `redirect_chain` | 重定向 Location 列表 |
| `elapsed_ms` | 请求耗时 |
| `derived` | 采集脚本从响应里读出的派生值（如 `industry_name`、`bk_code`、`data_date`） |
| `depends_on` | 依赖的上一个采集 key（行业 PE 第 2/3 步） |
| `derived_from` | 本步使用的派生参数及其来源 |
| `raw_file` | 原始字节文件名 |
| `note` | 采集口径说明（人读） |
| `meta_schema_version` | 本文件定义的版本号（当前 1） |

## 5. 复核方式

```bash
python -m unittest tests.test_s3_0_fixtures.TestMetadataContract -v
python3 -c "import json,sys; d=json.load(open('tests/fixtures/s3_0/em_sector_flow.meta.json')); \
  print([k for k in ('source','interface','url','params','method','requested_at', \
  'http_status','content_type','bytes','data_date','sha256','capture_env','outcome') if k not in d])"
# 期望输出：[]（空列表 = 13 项齐全）
```
