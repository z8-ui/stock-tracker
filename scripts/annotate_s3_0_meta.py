#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3-0 元数据澄清：为 8 份 meta 补齐 13 项口径（离线，不联网、不动 .raw）。

背景：任务书要求每份报文记录 13 项，其中"采集环境"与"采集结果及异常说明"
原先只写在 manifest / status 里，造成"13 项要求 vs 12 个必填键"的口径不一致。
本脚本做三件事，全部可重放：

  1. 每份 meta 增加 capture_env（与 manifest.capture_env 完全相同的副本）
     与 capture_env_ref（指回 manifest 的哪个位置）；
  2. 每份 meta 显式写出 outcome（采集结果及异常说明，纯事实描述）与 data_date（无则 null）；
  3. 写 meta_schema 版本号，便于以后加字段时区分。

约束：不改 .raw；不改任何既有 meta 字段的值（sha256/bytes/http_status 原样）；
不覆盖 manifest，只读它。
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "tests", "fixtures", "s3_0")

REQUIRED_13 = (
    "source",          # 数据源名称
    "interface",       # 接口名称
    "url",             # 完整 URL
    "params",          # 请求参数
    "method",          # HTTP 方法
    "requested_at",    # 请求时间
    "http_status",     # HTTP 状态码
    "content_type",    # Content-Type
    "bytes",           # 响应字节数
    "data_date",       # 数据所属日期（无则 null）
    "sha256",          # 原始报文摘要
    "capture_env",     # 采集环境（本文件内联 + 指回 manifest）
    "outcome",         # 采集结果及异常说明
)


def outcome_of(rec):
    """把 status/bytes/reason 翻译成一句人可读的事实，不美化。"""
    status = rec.get("status")
    n = rec.get("bytes")
    if status == "CAPTURED":
        return f"采集成功：HTTP {rec.get('http_status')}，{n} 字节，无异常"
    if status == "NOT_CAPTURED":
        return f"未采集：{rec.get('reason') or '原因未记录'}"
    return f"异常：status={status}，HTTP {rec.get('http_status')}，{n} 字节"


def main():
    with open(os.path.join(FIX, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    env = manifest["capture_env"]

    changed = 0
    for item in manifest["items"]:
        key = item["key"]
        path = os.path.join(FIX, key + ".meta.json")
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)

        before = dict(rec)
        rec["data_date"] = rec.get("data_date")      # 必须是显式存在的键，值可以是 null
        rec["capture_env"] = dict(env)
        rec["capture_env_ref"] = "manifest.json#capture_env"
        rec["outcome"] = outcome_of(rec)
        rec["meta_schema_version"] = 1

        missing = [k for k in REQUIRED_13 if k not in rec]
        if missing:
            raise SystemExit(f"{key} 仍缺字段：{missing}")

        # 保护：只允许新增键，不允许改动既有键的值
        for k, v in before.items():
            if k in ("data_date",):
                continue
            if rec.get(k) != v:
                raise SystemExit(f"{key} 字段 {k} 被意外改动，拒绝写入")

        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        changed += 1
        print(f"[annotated] {key:<32} outcome={rec['outcome']}")

    print(f"\n共更新 {changed} 份 meta；13 项字段：{', '.join(REQUIRED_13)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
