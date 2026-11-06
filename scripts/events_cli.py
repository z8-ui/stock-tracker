#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""政策事件中心命令行（不打开网页也能录事件）

用法示例：

    python3 scripts/events_cli.py seed                       # 把 policy_notes.py 老字典导入
    python3 scripts/events_cli.py stats                      # 库里有多少事件/关联
    python3 scripts/events_cli.py list                       # 最近 20 条
    python3 scripts/events_cli.py list --code 000657         # 某只股票挂的事件
    python3 scripts/events_cli.py list --industry 小金属

    # 手动录入（自己已经写好四要素）
    python3 scripts/events_cli.py add --date 2026-11-07 --title "某部委发布稀土配额" \
        --summary "配额收紧，供给端利好" --direction 利好 --source "财联社" --stock 000657

    # 粘一段新闻让模型抽（先预览，加 --yes 才写库）
    python3 scripts/events_cli.py add-text --file news.txt --stock 000657 --industry 小金属
    python3 scripts/events_cli.py add-text --text "……" --stock 000657 --yes

设计原则：AI 抽出来的东西一律先打印再落库，人看一眼才写——政策事件写错比没有更糟。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events_store  # noqa: E402


def cmd_seed(args):
    created, linked = events_store.seed_from_static(verbose=True)
    print("  当前库:", events_store.stats())
    return 0


def cmd_stats(args):
    s = events_store.stats()
    print(f"  事件 {s['events']} 条 | 关联 {s['links']} 条 | 覆盖个股 {s['stocks']} 只 | 行业 {s['industries']} 个")
    if s.get("orphan_links"):
        print(f"  [!] 孤儿关联 {s['orphan_links']} 条（event 已删但 link 残留，可跑 repair 清理）")
    print(f"  库文件: {s['db']}")
    return 0


def cmd_list(args):
    rows = events_store.list_events(limit=args.limit, code=args.code, industry=args.industry)
    if not rows:
        print("  （没有匹配的事件）")
        return 0
    print(f"  共 {len(rows)} 条（按日期倒序）")
    for r in rows:
        print(f"  {r['event_date']}  [{r['direction']}]  {r['title']}")
        print(f"      标的: {r['targets'] or '-'}   来源: {r['source'] or '-'}")
        if r["summary"]:
            print(f"      影响: {r['summary'][:80]}{'…' if len(r['summary']) > 80 else ''}")
    return 0


def cmd_add(args):
    eid, created, links = events_store.add_event(
        args.date, args.title, args.summary, args.direction, args.source,
        args.source_url, stocks=args.stock, industries=args.industry)
    print(f"  [{'新建' if created else '已存在(仅补关联)'}] event_id={eid} 新增关联 {links} 条")
    print("  当前库:", events_store.stats())
    return 0


def cmd_add_text(args):
    text = args.text
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    if not text:
        print("[x] 需要 --text 或 --file", file=sys.stderr)
        return 2

    try:
        event, usage = events_store.extract_event(text)
    except Exception as e:
        print(f"[x] 抽取失败: {e}", file=sys.stderr)
        return 1

    print("  —— 模型抽取结果（还没写库）——")
    for k in ("date", "title", "direction", "source"):
        print(f"  {k:<9}: {event[k]}")
    print(f"  summary  : {event['summary']}")
    print(f"  标的     : stock={args.stock or '-'} industry={args.industry or '-'}")
    if usage:
        print(f"  token    : prompt {usage.get('prompt_tokens')} + completion {usage.get('completion_tokens')}")

    if not args.yes:
        print("\n  预览模式：确认无误后加 --yes 再跑一次才会写库。")
        return 0

    eid, created, links = events_store.add_event(
        event["date"], event["title"], event["summary"], event["direction"],
        event["source"], "", stocks=args.stock, industries=args.industry)
    print(f"\n  [{'新建' if created else '已存在'}] event_id={eid} 新增关联 {links} 条")
    return 0


def cmd_archive(args):
    conn = events_store.connect()
    row = conn.execute("SELECT id, title FROM events WHERE id=?", (args.id,)).fetchone()
    if not row:
        print(f"[x] 没有 id={args.id} 的事件", file=sys.stderr)
        conn.close()
        return 1
    conn.execute("UPDATE events SET status=? WHERE id=?", (args.status, args.id))
    conn.commit()
    conn.close()
    print(f"  [v] event_id={args.id} 状态改为 {args.status}：{row['title'][:40]}")
    return 0


def cmd_repair(args):
    conn = events_store.connect()
    n = conn.execute("DELETE FROM event_links WHERE event_id NOT IN (SELECT id FROM events)").rowcount
    conn.commit()
    conn.close()
    print(f"  [v] 清理孤儿关联 {n} 条")
    return 0


def main():
    ap = argparse.ArgumentParser(description="政策事件中心 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("seed", help="从 policy_notes.py 导入老字典（幂等）")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("stats", help="库内统计")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("list", help="列出事件")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--code", help="只看某只股票")
    p.add_argument("--industry", help="只看某个行业关键词")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="手动录入一条")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--title", required=True)
    p.add_argument("--summary", default="")
    p.add_argument("--direction", default="中性", choices=list(events_store.DIRECTIONS))
    p.add_argument("--source", default="")
    p.add_argument("--source-url", default="")
    p.add_argument("--stock", action="append", help="可重复，如 --stock 000657 --stock 600549")
    p.add_argument("--industry", action="append", help="行业关键词，如 --industry 小金属")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("add-text", help="粘一段新闻，让模型抽成事件（预览后 --yes 落库）")
    p.add_argument("--text")
    p.add_argument("--file")
    p.add_argument("--stock", action="append")
    p.add_argument("--industry", action="append")
    p.add_argument("--yes", action="store_true", help="确认写库")
    p.set_defaults(func=cmd_add_text)

    p = sub.add_parser("archive", help="归档/恢复一条事件（不删数据）")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--status", default="archived", choices=["archived", "active"])
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("repair", help="清理孤儿关联（event 已删、link 残留）")
    p.set_defaults(func=cmd_repair)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
