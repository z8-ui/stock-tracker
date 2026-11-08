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
    try:
        created, linked = events_store.seed_from_static(verbose=True)
    except Exception as exc:                      # 不吞异常，但要说清「没写进去」
        print(f"  [!] seed 失败：本次没有留下任何半截数据（事务已回滚或尚未写入）")
        print(f"      原因: {type(exc).__name__}: {exc}")
        print(f"      库文件: {events_store.db_path()}")
        return 1
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
        occurred_at=args.date, title=args.title, summary=args.summary,
        direction=args.direction, source=args.source, source_url=args.source_url,
        stocks=args.stock, industries=args.industry,
        published_at=getattr(args, "published_at", None),
        review_status="approved")     # 人工手动录入 = 已审核
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
        event, usage = events_store.extract_event(
            text, published_at=getattr(args, "published_at", None))
    except Exception as e:
        print(f"[x] 抽取失败: {e}", file=sys.stderr)
        return 1

    print("  —— 模型抽取结果（还没写库）——")
    print(f"  occurred_at : {event['occurred_at'] or '未知（原文未写明，不补今天）'}")
    print(f"  published_at: {event['published_at'] or '未知'}")
    print(f"  collected_at: {event['collected_at']}")
    for k in ("title", "direction", "source"):
        print(f"  {k:<9}: {event[k]}")
    print(f"  summary  : {event['summary']}")
    print(f"  标的     : stock={args.stock or '-'} industry={args.industry or '-'}")
    if usage:
        print(f"  token    : prompt {usage.get('prompt_tokens')} + completion {usage.get('completion_tokens')}")

    if not args.yes:
        print("\n  预览模式：确认无误后加 --yes 再跑一次才会写库。")
        return 0

    eid, created, links = events_store.add_event(
        occurred_at=event["occurred_at"], title=event["title"], summary=event["summary"],
        direction=event["direction"], source=event["source"], source_url="",
        stocks=args.stock, industries=args.industry,
        published_at=event["published_at"], collected_at=event["collected_at"],
        review_status="approved")     # --yes 已人工确认
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


def cmd_link(args):
    """给已有事件补挂标的（录入时忘了写行业/漏了某只股票）"""
    conn = events_store.connect()
    row = conn.execute("SELECT title FROM events WHERE id=?", (args.id,)).fetchone()
    if not row:
        print(f"[x] 没有 id={args.id} 的事件", file=sys.stderr)
        conn.close()
        return 1
    added = 0
    for ttype, keys in (("stock", args.stock or ()), ("industry", args.industry or ())):
        for k in keys:
            try:
                conn.execute("INSERT INTO event_links (event_id,target_type,target_key) VALUES (?,?,?)",
                             (args.id, ttype, str(k).strip()))
                added += 1
            except Exception:
                pass
    conn.commit()
    conn.close()
    print(f"  [v] event_id={args.id} 新增关联 {added} 条：{row['title'][:40]}")
    return 0


def cmd_feed(args):
    """抓快讯 → 关键词筛 → 打印候选；--pick 选中后走 AI 抽取，--yes 才写库"""
    import event_feed

    items, errors, ok_sources = event_feed.fetch_headlines(
        sources=args.source or None, limit=args.limit)
    for e in errors:
        print(f"  [!] 抓取失败 {e}", file=sys.stderr)
    if ok_sources == 0:
        # 全部源都失败 ≠ “今天没新闻”：必须让 cron/人看到失败
        print("[x] 所有新闻源都抓取失败，本次采集无有效结果", file=sys.stderr)
        return 3
    if errors:
        print(f"  [!] {len(errors)} 个源失败，{ok_sources} 个源成功（继续处理）", file=sys.stderr)

    cands = event_feed.screen(items, args.keyword)
    cands, merged = event_feed.dedupe(cands)
    if args.strong:
        cands = [c for c in cands if c["weight"] >= 2]
    if not args.all:
        # 只排除人工已处理（approved/rejected/ingested）；
        # pending 与 extract_failed 必须继续出现在待审核列表里
        done = event_feed.seen_fps()
        cands = [c for c in cands if c["fp"] not in done]
    cands.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    cands.sort(key=lambda x: -x["weight"])          # 稳定排序：先强相关，再按时间倒序

    strong_n = sum(1 for c in cands if c["weight"] >= 2)
    print(f"  抓取 {len(items)} 条 | 候选 {len(cands)} 条"
          f"（强相关 {strong_n} / 弱相关 {len(cands) - strong_n}，合并近似标题 {merged} 条）"
          + ("" if args.all else "，已剔除看过的"))
    if not cands:
        print("  没有新候选。想重新看已筛过的加 --all；想换词加 --keyword。")
        return 0

    for i, it in enumerate(cands, 1):
        tag = "!" if it["weight"] >= 2 else "·"
        extra = f"  自选股 {','.join(it['codes'])}" if it.get("codes") else ""
        dup = f"  x{it['dup_count']}" if it.get("dup_count", 1) > 1 else ""
        print(f"  [{i}]{tag} {it['published_at']} {it['source']}  ← {','.join(it['hits'])}{extra}{dup}")
        print(f"      {it['title']}")
    print("  （! = 政策/行业/自选股相关，· = 公司或市场面）")

    if not args.pick:
        if args.dry_run:
            print("\n  --dry-run：不记录看过状态。")
        else:
            n = event_feed.mark(cands, process="pending")
            print(f"\n  {n} 条已进入待审核队列；未审核的不会因为被展示就消失。")
        print("  要把某条转成事件：--pick 序号 --stock 代码 [--industry 行业] [--yes]")
        return 0

    if getattr(args, "reject", None):
        idxs = []
        for part in args.reject.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit() or not (1 <= int(part) <= len(cands)):
                print(f"[x] 序号越界: {part}", file=sys.stderr)
                return 2
            idxs.append(int(part))
        n = event_feed.mark([cands[i - 1] for i in idxs], process="pending", review="rejected")
        print(f"  {n} 条已标记为 rejected（人工驳回，不会自动入库）")
        return 0

    idxs = []
    for part in args.pick.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not (1 <= int(part) <= len(cands)):
            print(f"[x] 序号越界: {part}", file=sys.stderr)
            return 2
        idxs.append(int(part))

    chosen, failed = [], []
    for idx in idxs:
        it = cands[idx - 1]
        print(f"\n  —— 抽取第 {idx} 条 ——")
        print(f"  原标题: {it['title']}")
        try:
            event, usage = events_store.extract_event(
                event_feed.to_event_text(it), published_at=it.get("published_at"))
        except Exception as e:
            print(f"  [x] 抽取失败: {e}", file=sys.stderr)
            failed.append(it)
            continue
        print(f"  date     : {event['date']}")
        print(f"  title    : {event['title']}")
        print(f"  direction: {event['direction']}")
        print(f"  source   : {event['source']}")
        print(f"  summary  : {event['summary']}")
        if usage:
            print(f"  token    : {usage.get('prompt_tokens')} + {usage.get('completion_tokens')}")
        chosen.append((it, event))

    if not chosen:
        # 抽取全失败：记 extract_failed，不记 skipped，下次还会提出来
        if failed:
            event_feed.mark(failed, "extract_failed")
        return 1
    if not args.yes:
        print("\n  预览模式：确认无误后加 --yes 才会写库。")
        return 0

    for it, ev in chosen:
        eid, created, links = events_store.add_event(
            occurred_at=ev["occurred_at"], title=ev["title"], summary=ev["summary"],
            direction=ev["direction"], source=ev["source"], source_url=it["url"],
            stocks=args.stock, industries=args.industry,
            published_at=ev["published_at"], collected_at=ev["collected_at"],
            review_status="approved")  # --pick --yes = 人工通过
        # 人工 --pick --yes = approved；process=ingested 带 event_id
        event_feed.mark([it], process="ingested", review="approved", event_id=eid)
        print(f"  [{'新建' if created else '已存在'}] event_id={eid} 关联 {links} 条 ← {it['title'][:30]}")

    if failed:
        event_feed.mark(failed, process="extract_failed")
        print(f"  [!] {len(failed)} 条抽取失败，已记为 extract_failed（下次 feed 会重新出现）")

    # 其余候选保持 pending：没被选中不代表被驳回，下次仍要出现
    print("  当前库:", events_store.stats())
    print("  候选状态:", event_feed.decision_stats())
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
    p.add_argument("--date", help="事件发生日期 YYYY-MM-DD；不知道就别填（不补今天）")
    p.add_argument("--published-at", help="来源发布时间 YYYY-MM-DD[ HH:MM]")
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
    p.add_argument("--published-at", help="来源发布时间 YYYY-MM-DD[ HH:MM]")
    p.add_argument("--yes", action="store_true", help="确认写库")
    p.set_defaults(func=cmd_add_text)

    p = sub.add_parser("archive", help="归档/恢复一条事件（不删数据）")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--status", default="archived", choices=["archived", "active"])
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("repair", help="清理孤儿关联（event 已删、link 残留）")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("link", help="给已有事件补挂标的")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--stock", action="append")
    p.add_argument("--industry", action="append")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("feed", help="抓财经快讯 → 关键词筛 → 候选（--pick 转事件，--yes 才写库）")
    p.add_argument("--source", action="append", choices=["eastmoney", "sina"], help="可重复；默认两个源")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--keyword", action="append", help="额外筛词，可重复")
    p.add_argument("--all", action="store_true", help="包含已看过的")
    p.add_argument("--strong", action="store_true", help="只要政策/行业/自选股相关的")
    p.add_argument("--dry-run", action="store_true", help="不记录看过状态")
    p.add_argument("--pick", help="要转成事件的序号，如 2,5（= 人工通过，配合 --yes 入库）")
    p.add_argument("--reject", help="人工驳回的序号，如 3,7")
    p.add_argument("--stock", action="append")
    p.add_argument("--industry", action="append")
    p.add_argument("--published-at", help="来源发布时间 YYYY-MM-DD[ HH:MM]")
    p.add_argument("--yes", action="store_true", help="确认写库")
    p.set_defaults(func=cmd_feed)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
