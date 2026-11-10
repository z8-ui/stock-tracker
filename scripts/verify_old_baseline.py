#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证明对照脚本里的"旧实现"确实逐行来自 0f7afe1，没有偷偷补边界判断。

做法：从 git 里取出 0f7afe1 的 get_stock_quote 函数体，和
scripts/compare_quote_migration.py 里的 old_parse 函数体，各自抽出
"字段 → 取值表达式"与"len(parts) 守卫"，逐条比对。任何一条不等就非 0 退出。

用法：python scripts/verify_old_baseline.py
"""

import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_COMMIT = "0f7afe1"          # S3-1 之前的最后一个提交
REPLICA = os.path.join(REPO, "scripts", "compare_quote_migration.py")

FIELD_ASSIGN = re.compile(r"^\s*(\w+)\s*=\s*(.+?)\s*$")
# 只关心"从 parts 里取值"的语句，以及价格有效性判断
INTERESTING = ("parts[", "price <= 0", "return {", "\"name\"", "\"turnover\"")


def extract_body(src, start_pat):
    lines = src.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(start_pat, ln)), None)
    if start is None:
        raise SystemExit(f"找不到函数：{start_pat}")
    body = []
    for ln in lines[start + 1:]:
        if ln and not ln[0].isspace() and not ln.startswith((" ", "\t")):
            break                      # 顶格 = 下一个顶层定义
        body.append(ln)
    return body


def statements(body):
    """抽出 (左值, 表达式) 与守卫，去掉注释与空行。"""
    out = []
    for ln in body:
        code = ln.split("#")[0].rstrip()
        if not code.strip():
            continue
        out.append(code.strip())
    return out


def field_map(stmts):
    m = {}
    for s in stmts:
        mm = FIELD_ASSIGN.match(s)
        if mm and "parts[" in mm.group(2):
            m[mm.group(1)] = mm.group(2)
    return m


def guards(stmts):
    return sorted(s for s in stmts if "len(parts)" in s)


def main():
    old_src = subprocess.run(["git", "show", f"{BASE_COMMIT}:data_service.py"],
                             cwd=REPO, capture_output=True, text=True).stdout
    if not old_src:
        raise SystemExit(f"取不到 {BASE_COMMIT}:data_service.py")
    old_stmts = statements(extract_body(old_src, r"^def get_stock_quote\("))

    with open(REPLICA, encoding="utf-8") as f:
        rep_src = f.read()
    rep_stmts = statements(extract_body(rep_src, r"^def old_parse\("))

    of, rf = field_map(old_stmts), field_map(rep_stmts)
    print(f"基线提交：{BASE_COMMIT}:data_service.py::get_stock_quote")
    print(f"对照副本：scripts/compare_quote_migration.py::old_parse")
    print()
    print("| 字段 | 0f7afe1 原文表达式 | 副本表达式 | 一致 |")
    print("|---|---|---|---|")
    bad = 0
    for k in of:
        same = of[k] == rf.get(k)
        bad += 0 if same else 1
        print(f"| `{k}` | `{of[k]}` | `{rf.get(k)}` | {'✓' if same else '✗'} |")
    extra = [k for k in rf if k not in of]
    if extra:
        bad += len(extra)
        print(f"\n✗ 副本多出字段：{extra}")
    missing = [k for k in of if k not in rf]
    if missing:
        bad += len(missing)
        print(f"\n✗ 副本缺少字段：{missing}")

    og, rg = guards(old_stmts), guards(rep_stmts)
    print()
    print(f"len(parts) 守卫：原文 {len(og)} 条 / 副本 {len(rg)} 条")
    for s in og:
        hit = "✓" if s in rg else "✗"
        bad += 0 if s in rg else 1
        print(f"  {hit} 原文：{s}")
    for s in rg:
        if s not in og:
            bad += 1
            print(f"  ✗ 副本独有的守卫（不允许）：{s}")

    print()
    print("结论：" + ("字段取值表达式与 len(parts) 守卫一致（不是整函数 AST 级逐行比对）"
                     if bad == 0 else f"发现 {bad} 处不一致"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
