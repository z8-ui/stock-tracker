# -*- coding: utf-8 -*-
"""S3-3 证据脚本：板块 clist 接口的分页 / pz 上限 / total 口径 / PB 分布探针。

只读 GET，不改任何代码与夹具；输出可直接贴进评审材料。

用法：
    python scripts/probe_sector_pagination.py            # 默认 5 页 pz=100 + 边界探针
    python scripts/probe_sector_pagination.py --pages 9  # 按 pz=60 抓 9 页验证 496

结论口径（本脚本只给数字，不下业务判断）：
  1) total 字段在各页是否恒定；
  2) pn 增大到超出范围时返回什么；
  3) pz 是否有服务端上限（比较响应字节与行数）；
  4) 多页合并后唯一板块数是否等于 total；
  5) f23(PB)/f37 在各板块类型下是否为 "-"。
"""

import argparse
import collections
import json
import sys
import urllib.parse
import urllib.request

BASE = "https://push2.eastmoney.com/api/qt/clist/get"
UT = "bd1d9ddb04089700cf9c27f6f7426281"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"


def get(fields, fs="m:90+t:2", **params):
    q = {"ut": UT, "fltt": "2", "invt": "2", "np": "1", "fid": "f3",
         "fs": fs, "fields": fields}
    q.update({k: str(v) for k, v in params.items()})
    url = BASE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://quote.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        status = r.status
    d = json.loads(raw.decode("utf-8"))
    data = d.get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    return {"status": status, "bytes": len(raw), "total": data.get("total"), "rows": diff}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=5, help="合并验证的页数")
    ap.add_argument("--pz", type=int, default=100, help="每页条数")
    args = ap.parse_args()

    print("=" * 72)
    print("A. 单页与越界页（fs=m:90+t:2 行业板块, 按 f3 涨跌幅降序）")
    for pn in (1, 2, args.pages, args.pages + 1, args.pages + 5):
        r = get("f12,f14,f2,f3,f62", pn=pn, pz=args.pz, po=1)
        first = r["rows"][0].get("f14") if r["rows"] else None
        print(f"  pn={pn:<3} pz={args.pz:<4} total={str(r['total']):<6} rows={len(r['rows']):<4} "
              f"{r['bytes']:>6}B first={first}")

    print("=" * 72)
    print("B. pz 服务端上限（pn=1）")
    for pz in (60, 100, 101, 200, 500):
        r = get("f12,f14,f3", pn=1, pz=pz, po=1)
        print(f"  pz={pz:<4} total={r['total']} rows={len(r['rows']):<4} bytes={r['bytes']}")

    print("=" * 72)
    print(f"C. 多页合并（pz={args.pz} × {args.pages} 页，按 f3 排序）")
    rows, totals = [], set()
    for pn in range(1, args.pages + 1):
        r = get("f12,f14,f2,f3,f62", pn=pn, pz=args.pz, po=1)
        totals.add(r["total"])
        rows += r["rows"]
    codes = [x.get("f12") for x in rows]
    names = [x.get("f14") for x in rows]
    dup = [k for k, v in collections.Counter(codes).items() if v > 1]
    print(f"  各页 total 集合={totals} 累计行数={len(rows)} 唯一代码={len(set(codes))} "
          f"唯一名称={len(set(names))} 重复代码={len(dup)}")
    print(f"  唯一代码数 == total ? {len(set(codes)) == (list(totals)[0] if len(totals) == 1 else -1)}")

    print("=" * 72)
    print("D. 估值字段分布（全量合并后）")
    rows = []
    for pn in range(1, args.pages + 1):
        rows += get("f12,f14,f9,f20,f23,f25,f37", pn=pn, pz=args.pz, po=1)["rows"]
    for f, desc in (("f9", "PE(动)"), ("f23", "PB"), ("f25", "PE(静?)"), ("f37", "?"), ("f20", "总市值")):
        vals = [x.get(f) for x in rows]
        ty = collections.Counter(type(v).__name__ for v in vals)
        nums = [v for v in vals if isinstance(v, (int, float))]
        print(f"  {f:<4}({desc:<6}) types={dict(ty)} '-'={sum(1 for v in vals if v == '-')} "
              f"neg={sum(1 for v in nums if v < 0)} zero={sum(1 for v in nums if v == 0)} "
              f"min={min(nums) if nums else None} max={max(nums) if nums else None}")

    print("=" * 72)
    print("E. PB(f23) 在不同板块类型下是否可用（pz=8 抽样）")
    for fs, label in (("m:90+t:2", "行业"), ("m:90+t:3", "概念"), ("m:90+t:1", "地域")):
        r = get("f12,f14,f9,f23", fs=fs, pn=1, pz=8, po=1)
        sample = [(x.get("f14"), x.get("f9"), x.get("f23")) for x in r["rows"][:3]]
        print(f"  fs={fs:<11}({label}) total={r['total']} 前三: {sample}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
