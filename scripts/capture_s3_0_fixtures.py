#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3-0 真实报文采集脚本（只采集，不解析业务语义，不修改任何业务代码）。

职责边界：
  - 只做 HTTP 请求 + 原样落盘 + 事实记录（URL/参数/状态码/字节/时间/sha256）；
  - 不调用 data_service / services.data.providers 的任何解析逻辑 ——
    采集物必须是"上游给了什么"，不能混入本系统的加工结果；
  - 不做重试：G4 要求每个接口只发 1 次请求（重定向由 requests 自动跟随并记录）；
  - 失败就如实写 NOT_CAPTURED / BLOCKED_HTTP_xxx，不伪造、不用别的接口顶替。

输出目录：tests/fixtures/s3_0/
  <key>.raw          原始响应字节，逐字节落盘，不格式化、不改字段
  <key>.meta.json    采集元数据（含 sha256、字节数、HTTP 状态、Content-Type、依赖关系）
  manifest.json      8 份采集物汇总 + sha256 清单 + 采集环境

用法：
  python3 scripts/capture_s3_0_fixtures.py            # 正常采集
  python3 scripts/capture_s3_0_fixtures.py --dry-run   # 只打印将请求的 URL
"""

import argparse
import hashlib
import json
import os
import platform
import socket
import sys
from datetime import datetime

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "tests", "fixtures", "s3_0")

UA_EM = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}
UA_TX = {"User-Agent": "Mozilla/5.0"}

CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
CLIST_BASE = {
    "pn": "1", "pz": "60", "po": "1", "np": "1",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": "2", "invt": "2", "fid": "f3", "fs": "m:90+t:2",
}

KLINE_START = "2026-09-01"
KLINE_END = "2026-11-10"

# 8 个采集对象：6 类接口，其中行业 PE 是三步链（3 份原始响应）
SPECS = [
    {
        "key": "tencent_quote_sh600519",
        "source": "tencent",
        "interface": "qt.gtimg.cn 实时快照（个股行情）",
        "method": "GET",
        "url": "http://qt.gtimg.cn/q=sh600519",
        "params": {},
        "headers": UA_TX,
        "encoding_hint": "gbk",
        "source_ref": "services/data/providers/tencent.py:35",
        "note": "腾讯快照不是 JSON，是 v_sh600519=\"...\" 的 ~ 分隔文本；按原始字节保存",
    },
    {
        "key": "em_sector_flow",
        "source": "eastmoney",
        "interface": "push2 clist 行业板块资金流（fs=m:90+t:2, fields=f14,f2,f3,f62）",
        "method": "GET",
        "url": CLIST,
        "params": dict(CLIST_BASE, fields="f14,f2,f3,f62"),
        "headers": UA_EM,
        "source_ref": "data_service.py:656-663",
        "note": "与板块估值同接口不同 fields；f62 单位为元",
    },
    {
        "key": "em_sector_valuation",
        "source": "eastmoney",
        "interface": "push2 clist 行业板块估值（fields=f14,f2,f3,f9,f20,f23,f25,f37）",
        "method": "GET",
        "url": CLIST,
        "params": dict(CLIST_BASE, fields="f14,f2,f3,f9,f20,f23,f25,f37"),
        "headers": UA_EM,
        "source_ref": "data_service.py:1327-1331",
        "note": "f9=动态PE f23=PB f20=总市值(元) f25/f37 目前未使用，先留证",
    },
    {
        "key": "em_f10_600519",
        "source": "eastmoney",
        "interface": "datacenter-web F10 主要财务指标（RPT_F10_FINANCE_MAINFINADATA）",
        "method": "GET",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get",
        "params": {
            "reportName": "RPT_F10_FINANCE_MAINFINADATA",
            "columns": "ALL",
            "filter": '(SECUCODE="600519.SH")',
            "pageNumber": "1", "pageSize": "1",
            "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        },
        "headers": UA_EM,
        "source_ref": "data_service.py:920-929",
        "note": "columns=ALL 字段极多，只记录用到的字段",
    },
    {
        "key": "em_industry_pe_step1_stock",
        "source": "eastmoney",
        "interface": "行业PE 第1步：个股 → 行业名（f57,f127）",
        "method": "GET",
        "url": "https://push2.eastmoney.com/api/qt/stock/get",
        "params": {"secid": "1.600519", "fields": "f57,f127"},
        "headers": UA_EM,
        "source_ref": "data_service.py:964-967",
        "note": "第2步的 input 必须取本响应里的 f127",
    },
    {
        "key": "em_industry_pe_step2_suggest",
        "source": "eastmoney",
        "interface": "行业PE 第2步：行业名 → 板块代码（suggest type=14）",
        "method": "GET",
        "url": "https://searchapi.eastmoney.com/api/suggest/get",
        "params": {"input": None, "type": "14", "count": "5"},   # input 由 step1 派生
        "headers": UA_EM,
        "source_ref": "data_service.py:973-976",
        "depends_on": "em_industry_pe_step1_stock",
        "derive": "industry_name",
        "note": "取第一个 BK 开头的 Code；type=14 表示板块",
    },
    {
        "key": "em_industry_pe_step3_ulist",
        "source": "eastmoney",
        "interface": "行业PE 第3步：板块 → 动态PE（f9，放大100倍）",
        "method": "GET",
        "url": "https://push2.eastmoney.com/api/qt/ulist.np/get",
        "params": {"secids": None, "fields": "f12,f14,f9"},      # secids 由 step2 派生
        "headers": UA_EM,
        "source_ref": "data_service.py:982-985",
        "depends_on": "em_industry_pe_step2_suggest",
        "derive": "bk_code",
        "note": "f9 需 /100 才是 PE",
    },
    {
        "key": "tencent_index_kline_sh000001",
        "source": "tencent",
        "interface": "web.ifzq fqkline 指数日K（sh000001, qfq）",
        "method": "GET",
        "url": "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "params": {"param": f"sh000001,day,{KLINE_START},{KLINE_END},30,qfq"},
        "headers": UA_TX,
        "source_ref": "data_service.py:1164-1167",
        "note": "http 会 302 到 https；redirect 链记进 meta.final_url / redirect_chain",
    },
]


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def json_paths(obj, prefix="", depth=0, out=None):
    """列出 JSON 的所有路径（限深度，避免 F10 的 columns=ALL 刷屏）。"""
    if out is None:
        out = []
    if depth > 3:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out.append(p)
            json_paths(v, p, depth + 1, out)
    elif isinstance(obj, list) and obj:
        json_paths(obj[0], prefix + "[0]", depth + 1, out)
    return out


def derive(key, raw_bytes, spec):
    """从原始响应里取下一步需要的派生值 + 数据日期。只读，不改原始文件。"""
    info = {}
    try:
        if spec.get("encoding_hint") == "gbk":
            text = raw_bytes.decode("gbk", errors="replace")
            parts = text.split("~")
            info["format"] = "tencent_tilde_text"
            info["field_count"] = len(parts)
            info["data_date"] = parts[30][:8] if len(parts) > 30 and parts[30] else None
            info["sample_head"] = text[:60]
            return info
        doc = json.loads(raw_bytes.decode("utf-8"))
    except Exception as e:
        info["derive_error"] = f"{type(e).__name__}: {e}"
        return info

    if key == "em_industry_pe_step1_stock":
        info["industry_name"] = (doc.get("data") or {}).get("f127")
        info["data_date"] = None
    elif key == "em_industry_pe_step2_suggest":
        rows = (doc.get("QuotationCodeTable") or {}).get("Data") or []
        info["bk_code"] = next((str(it.get("Code")) for it in rows
                                if str(it.get("Code") or "").startswith("BK")), None)
        info["suggest_rows"] = len(rows)
        info["data_date"] = None
    elif key == "em_industry_pe_step3_ulist":
        diff = (doc.get("data") or {}).get("diff") or []
        info["row_count"] = len(diff)
        if diff:
            info["f9_raw"] = diff[0].get("f9")
        info["data_date"] = None
    elif key == "em_f10_600519":
        rows = ((doc.get("result") or {}).get("data")) or []
        info["row_count"] = len(rows)
        if rows:
            info["data_date"] = (rows[0].get("REPORT_DATE") or "")[:10] or None
            info["report_name"] = rows[0].get("REPORT_DATE_NAME")
    elif key.startswith("em_sector"):
        diff = (doc.get("data") or {}).get("diff") or []
        info["row_count"] = len(diff)
        info["data_date"] = None          # 该接口不返回数据日期，只有采集时间
        if diff:
            info["sample_row_keys"] = sorted(diff[0].keys())
    elif key == "tencent_index_kline_sh000001":
        d = (doc.get("data") or {}).get("sh000001") or {}
        klines = d.get("day") or d.get("qfqday") or []
        info["row_count"] = len(klines)
        info["data_date"] = klines[-1][0] if klines else None
        info["kline_field_len"] = len(klines[-1]) if klines else None
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="",
                    help="只采集指定 key（逗号分隔）；依赖值从已落盘的 meta 读取，不重复请求")
    args = ap.parse_args()

    if args.dry_run:
        for s in SPECS:
            print(f"{s['key']:<32} {s['method']} {s['url']} {s['params']}")
        return 0

    only = [k.strip() for k in args.only.split(",") if k.strip()]
    if only:
        unknown = set(only) - {s["key"] for s in SPECS}
        if unknown:
            print(f"未知 key: {unknown}")
            return 1
    selected = [s for s in SPECS if not only or s["key"] in only]

    os.makedirs(OUT_DIR, exist_ok=True)
    sess = requests.Session()
    captured = {}
    records = []

    # 已存在的采集物：只读 meta，不重发请求（保证"每接口 1 次"的纪律）
    for spec in SPECS:
        p = os.path.join(OUT_DIR, spec["key"] + ".meta.json")
        if spec["key"] in [s["key"] for s in selected] or not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            old = json.load(f)
        records.append(old)
        if old.get("status") == "CAPTURED":
            captured[spec["key"]] = old.get("derived") or {}

    for spec in selected:
        key = spec["key"]
        rec = {
            "key": key,
            "source": spec["source"],
            "interface": spec["interface"],
            "method": spec["method"],
            "url": spec["url"],
            "params": spec["params"],
            "headers": spec["headers"],
            "source_ref": spec["source_ref"],
            "note": spec.get("note", ""),
            "depends_on": spec.get("depends_on"),
            "requested_at": now_iso(),
        }

        # 依赖派生：上一步没成功就明确 NOT_CAPTURED，不猜参数
        if spec.get("depends_on"):
            prev = captured.get(spec["depends_on"]) or {}
            val = prev.get(spec.get("derive"))
            if not val:
                rec.update({"status": "NOT_CAPTURED",
                            "reason": f"上游 {spec['depends_on']} 未产出 {spec.get('derive')}，不猜参数"})
                rec["finished_at"] = now_iso()
                records.append(rec)
                print(f"[NOT CAPTURED] {key}: 依赖缺失")
                continue
            if "input" in spec["params"] and spec["params"]["input"] is None:
                spec["params"]["input"] = val
            if "secids" in spec["params"] and spec["params"]["secids"] is None:
                spec["params"]["secids"] = f"90.{val}"
            rec["params"] = spec["params"]
            rec["derived_from"] = {spec["derive"]: val}

        t0 = datetime.now()
        try:
            resp = sess.request(spec["method"], spec["url"], params=spec["params"],
                                headers=spec["headers"], timeout=20, allow_redirects=True)
            elapsed_ms = int((datetime.now() - t0).total_seconds() * 1000)
            body = resp.content
            raw_path = os.path.join(OUT_DIR, key + ".raw")
            with open(raw_path, "wb") as f:      # 逐字节落盘，不做任何格式化
                f.write(body)

            rec.update({
                "http_status": resp.status_code,
                "content_type": resp.headers.get("Content-Type", ""),
                "content_encoding": resp.headers.get("Content-Encoding", ""),
                "bytes": len(body),
                "sha256": sha256_bytes(body),
                "elapsed_ms": elapsed_ms,
                "final_url": resp.url,
                "redirect_chain": [h.headers.get("Location") for h in resp.history if h.headers.get("Location")],
                "raw_file": os.path.basename(raw_path),
            })
            # 状态只用事实描述，不美化
            if resp.status_code == 200 and len(body) > 0:
                rec["status"] = "CAPTURED"
            elif resp.status_code == 200:
                rec["status"] = "BLOCKED_EMPTY_BODY"
            else:
                rec["status"] = f"BLOCKED_HTTP_{resp.status_code}"

            info = derive(key, body, spec)
            rec["derived"] = info
            rec["data_date"] = info.get("data_date")
            captured[key] = info

        except Exception as e:
            rec.update({"status": "NOT_CAPTURED",
                        "reason": f"{type(e).__name__}: {e}",
                        "http_status": None, "bytes": 0, "sha256": None})
            print(f"[ERROR] {key}: {type(e).__name__}: {e}")

        rec["finished_at"] = now_iso()
        with open(os.path.join(OUT_DIR, key + ".meta.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        records.append(rec)
        print(f"[{rec['status']}] {key:<32} {rec.get('http_status')} "
              f"{rec.get('bytes', 0):>7}B {rec.get('sha256') or '-'}")

    head = ""
    order = {s["key"]: i for i, s in enumerate(SPECS)}
    records.sort(key=lambda r: order.get(r["key"], 99))
    git_head_path = os.path.join(REPO, ".git", "HEAD")
    try:
        with open(git_head_path) as f:
            head = f.read().strip()
        if head.startswith("ref: "):
            with open(os.path.join(REPO, ".git", head[5:].strip())) as f:
                head = f.read().strip()
    except Exception:
        pass

    manifest = {
        "stage": "S3-0",
        "purpose": "真实上游报文采集（离线夹具基线），不迁移 Provider、不改业务逻辑",
        "captured_at": now_iso(),
        "capture_env": {
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "requests": requests.__version__,
            "repo": REPO,
            "git_head": head,
            "cwd": os.getcwd(),
        },
        "counts": {
            "targets": len(SPECS),
            "captured": sum(1 for r in records if r["status"] == "CAPTURED"),
            "not_captured": sum(1 for r in records if r["status"] != "CAPTURED"),
        },
        "items": [
            {
                "key": r["key"], "source": r["source"], "interface": r["interface"],
                "status": r["status"], "url": r["url"], "params": r["params"],
                "http_status": r.get("http_status"), "content_type": r.get("content_type"),
                "bytes": r.get("bytes"), "sha256": r.get("sha256"),
                "raw_file": r.get("raw_file"), "data_date": r.get("data_date"),
                "requested_at": r["requested_at"], "depends_on": r.get("depends_on"),
            } for r in records
        ],
    }
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 78)
    print(f"采集 {manifest['counts']['captured']}/{len(SPECS)} 成功，"
          f"{manifest['counts']['not_captured']} 未采集")
    print(f"输出目录：{OUT_DIR}")
    return 0 if manifest["counts"]["not_captured"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
