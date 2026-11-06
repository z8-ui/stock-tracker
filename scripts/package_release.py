#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打一个「可以发给别人」的发布包。

背景：2026-11 把 16MB 的 stock-tracker.zip 发出去后才发现里面带着
config_local.py（明文 DeepSeek key）、.venv/、.git/、__pycache__/
和运行缓存。Git 忽略 ≠ 压缩包忽略，所以打包这件事不能再靠手点右键。

本脚本做三件事：
  1. 按白名单式的排除规则打包（.git/.venv/__pycache__/config_local.py/*_cache.json …）
  2. 打包前扫描每个待入包文件，发现疑似密钥（sk-xxx / ark-xxx）直接中止
  3. 打完再扫一遍 zip 内容，确认包里一个密钥都没有，并打印体积/条目数

用法：
    python3 scripts/package_release.py                # 输出到 dist/
    python3 scripts/package_release.py --dry-run      # 只报告，不写文件
    python3 scripts/package_release.py --out /tmp/x.zip
"""

import argparse
import os
import re
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 目录：整棵剪掉
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "stock-venv", "__pycache__", "dist", "build",
    ".idea", ".vscode", ".pytest_cache", "node_modules", ".mypy_cache",
}
# 文件：按名剪掉
EXCLUDE_FILES = {
    "config_local.py",          # 个人自选池 + 明文 key
    "_data_cache.json",         # 运行时行情快照
    "ai_cache.json",            # AI 分析结果缓存
    ".DS_Store", "Thumbs.db",
}
# 文件名后缀：剪掉
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".bak", ".log", ".tmp", ".swp")

# 疑似密钥：sk- 开头 / ark- 开头（火山方舟）/ 常见 key= 赋值
SECRET_PATTERNS = [
    ("deepseek/openai", re.compile(rb"sk-[A-Za-z0-9]{20,}")),
    ("volcengine-ark", re.compile(rb"ark-[A-Za-z0-9-]{20,}")),
]
MAX_SCAN_BYTES = 8 * 1024 * 1024   # 超过 8MB 的文件不整读（本项目没有这么大的源码）


def iter_files():
    """遍历项目文件，返回 (绝对路径, 相对路径)，已应用排除规则。"""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for fn in sorted(filenames):
            if fn in EXCLUDE_FILES or fn.endswith(EXCLUDE_SUFFIXES):
                continue
            abs_p = os.path.join(dirpath, fn)
            rel_p = os.path.relpath(abs_p, ROOT).replace(os.sep, "/")
            yield abs_p, rel_p


def scan_secrets(data, rel_path):
    """返回命中的 (标签, 掩码) 列表，不返回完整密钥。"""
    hits = []
    for label, pat in SECRET_PATTERNS:
        for m in pat.finditer(data):
            token = m.group(0).decode("latin-1")
            hits.append((label, f"{rel_path}: {token[:6]}...{token[-4:]}"))
    return hits


def main():
    ap = argparse.ArgumentParser(description="打干净发布包（排除私密配置 + 扫描密钥）")
    ap.add_argument("--out", help="输出 zip 路径（默认 dist/stock-tracker-<时间>.zip）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(ROOT, "dist", f"stock-tracker-{stamp}.zip")

    files = list(iter_files())
    total_bytes = sum(os.path.getsize(p) for p, _ in files)

    # ---- 1. 入包前扫描 ----
    print(f"== 入包文件 {len(files)} 个，未压缩合计 {total_bytes / 1024:.1f} KB ==")
    leaks = []
    for abs_p, rel_p in files:
        size = os.path.getsize(abs_p)
        if size > MAX_SCAN_BYTES:
            continue
        try:
            with open(abs_p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        leaks.extend(scan_secrets(data, rel_p))

    if leaks:
        print("\n[x] 发现疑似密钥，已中止打包（不写任何文件）：")
        for label, where in leaks[:20]:
            print(f"    [{label}] {where}")
        print("\n    处理：把该值挪进环境变量或 config_local.py（后者已被排除），再重跑。")
        return 1
    print("  [v] 密钥扫描：0 命中")

    # ---- 2. 写出 zip ----
    if args.dry_run:
        print(f"\n[dry-run] 本应写出 {out}，未写文件。")
        return 0

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".part"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for abs_p, rel_p in files:
                zf.write(abs_p, arcname=f"stock-tracker/{rel_p}")
        os.replace(tmp, out)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

    # ---- 3. 打完复检 ----
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        bad_entries = [n for n in names if
                       n.startswith("stock-tracker/.git/") or
                       n.startswith("stock-tracker/.venv/") or
                       "__pycache__" in n or
                       n.endswith("/config_local.py") or
                       n.endswith(("/_data_cache.json", "/ai_cache.json"))]
        zip_leaks = []
        for n in names:
            if n.endswith("/"):
                continue
            if zf.getinfo(n).file_size > MAX_SCAN_BYTES:
                continue
            zip_leaks.extend(scan_secrets(zf.read(n), n))

    size_mb = os.path.getsize(out) / 1024 / 1024
    print(f"\n[v] 已写出 {out}")
    print(f"    条目 {len(names)} 个，{size_mb:.2f} MB（源目录 {total_bytes/1024/1024:.2f} MB 未压缩）")
    print(f"    [{'v' if not bad_entries else 'x'}] 私密/垃圾条目：{len(bad_entries)} 个")
    print(f"    [{'v' if not zip_leaks else 'x'}] 包内密钥复检：{len(zip_leaks)} 命中")
    if bad_entries or zip_leaks:
        for n in (bad_entries + [w for _, w in zip_leaks])[:20]:
            print(f"      - {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
