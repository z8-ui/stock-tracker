# -*- coding: utf-8 -*-
"""离线自验脚本（**只用 Python 标准库**，不需要 pip install，不需要网络）。

给"只能读代码、不能装依赖"的审查方用：在解压后的交付包目录里直接运行

    python scripts/verify_package.py --root . --manifest MANIFEST.sha256 --log testlog.log

它会自己完成这件事，并把每一条结论打印成 PASS/FAIL：

  1. 文件清单完整性：逐个文件复算 SHA256，与 MANIFEST 比对（证明包没被截断/改动）。
  2. 依赖无关的解析验证：注入一个假的 requests 模块（只因 Provider 顶部 import 了它），
     然后用 tests/fixtures/eastmoney/ 里的**真实采集报文**跑 Provider 的三种模式，
     把结果与我方声明的数值逐个比对 —— 这个过程不需要 flask、不需要 requests、不联网。
  3. secid 规则：600519/900901/000001/300750/指数/非法码，逐条比对。
  4. 差分：用本脚本内嵌的"迁移前实现"重算同一份报文，与 Provider 结果逐键比对，差异必须为 0。
  5. 测试日志：解析 unittest -v 日志，核对 Ran 数、OK/FAILED、以及 "... ok" 行数三者一致。

退出码 0 = 全部 PASS；非 0 = 有 FAIL（每条 FAIL 都会打印细节）。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import types

FIXTURES = os.path.join("tests", "fixtures", "eastmoney")

# ---- 我方声明的期望值（全部来自真实采集报文，见 CAPTURE.md）----
EXPECT = {
    "history_600519_last": {"super_large": -1.86, "large": -1.33,
                            "middle": 3.19, "small": -0.0},
    "history_600519_dates": 120,
    "history_000001_last": {"super_large": -0.66, "large": 1.44,
                            "middle": -0.66, "small": -0.12},
    "today_600519": {"main_force": -318894480.0, "super_large": -185607584.0,
                     "large": -133286896.0, "middle": 319003136.0,
                     "small": -108674.0},
    "snapshot_total": {"main_force": -524.97, "super_large": -233.21,
                       "large_order": -291.77, "middle_order": 45.83,
                       "small_order": 479.15},
    "secid": [("600519", "stock", "1.600519"), ("900901", "stock", "1.900901"),
              ("000001", "stock", "0.000001"), ("300750", "stock", "0.300750"),
              ("688981", "stock", "1.688981"), ("830799", "stock", "0.830799"),
              ("000001", "index", "1.000001"), ("399001", "index", "0.399001")],
    "secid_bad": ["700000", "60051", "abc", ""],
}

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  " + detail) if detail and not ok else ""))
    return ok


# --------------------------------------------------------------------- 工具
def install_requests_stub():
    """Provider 顶层 import requests；解析本身不需要它。缺依赖时注入空壳，禁止真发请求。"""
    try:
        import requests                    # noqa: F401
        return "real"
    except ImportError:
        mod = types.ModuleType("requests")

        class _Timeout(Exception):
            pass

        class _RequestException(Exception):
            pass

        def _boom(*a, **k):
            raise AssertionError("verify 脚本不应发起真实网络请求")

        mod.Timeout = _Timeout
        mod.RequestException = _RequestException
        mod.ConnectionError = _RequestException
        mod.get = _boom
        sys.modules["requests"] = mod
        return "stub"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(rel):
    with open(rel, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------- 1. 清单完整性
def verify_manifest(root, manifest_path):
    if not manifest_path:
        print("SKIP  文件清单（未提供 --manifest）")
        return
    if not os.path.exists(manifest_path):
        check("文件清单存在", False, manifest_path + " 不存在")
        return
    bad, missing, extra_checked = [], [], 0
    listed = set()
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            digest, _, rel = line.partition("  ")
            rel = rel.strip()
            listed.add(rel)
            path = os.path.join(root, rel)
            if not os.path.exists(path):
                missing.append(rel)
                continue
            extra_checked += 1
            if sha256_file(path) != digest:
                bad.append(rel)
    check("清单内文件全部存在", not missing, "缺: " + ", ".join(missing[:5]))
    check("清单内文件 SHA256 全部一致（%d 个）" % extra_checked, not bad,
          "不一致: " + ", ".join(bad[:5]))


# ------------------------------------------------------- 2/3/4 解析与差分
def legacy_history(klines, days=20):
    """迁移前实现（冻结副本），用于差分。"""
    def yi(v):
        try:
            return round(float(v) / 1e8, 2)
        except (TypeError, ValueError):
            return 0.0

    out = {"dates": [], "flows": {"super_large": [], "large": [], "middle": [], "small": []}}
    for row in [k.split(",") for k in klines][-days:]:
        if len(row) < 5:
            continue
        out["dates"].append(row[0][5:] if len(row[0]) >= 10 else row[0])
        out["flows"]["small"].append(yi(row[2]))
        out["flows"]["middle"].append(yi(row[3]))
        out["flows"]["large"].append(yi(row[4]))
        out["flows"]["super_large"].append(yi(row[5]) if len(row) > 5
                                            else round(yi(row[1]) - yi(row[4]), 2))
    out["unit"] = "亿元"
    out["is_mock"] = False
    return out


def diffs(a, b, path="", acc=None):
    if acc is None:
        acc = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            diffs(a.get(k), b.get(k), path + "." + str(k), acc)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            acc.append("%s: 长度 %d vs %d" % (path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            diffs(x, y, "%s[%d]" % (path, i), acc)
    elif a != b:
        acc.append("%s: %r vs %r" % (path, a, b))
    return acc


def verify_parsing(root):
    sys.path.insert(0, root)
    from services.data.normalizers import secid            # noqa: E402
    from services.data.providers.eastmoney_flow import EastMoneyFlowProvider   # noqa: E402

    p = EastMoneyFlowProvider()

    # —— secid ——
    ok = True
    for code, kind, want in EXPECT["secid"]:
        got = secid(code, kind)
        if got != want:
            ok = False
            print("      secid(%r,%r) = %r，期望 %r" % (code, kind, got, want))
    check("secid 规则矩阵（%d 条）" % len(EXPECT["secid"]), ok)

    ok = True
    for bad in EXPECT["secid_bad"]:
        try:
            secid(bad, "stock")
            ok = False
            print("      secid(%r) 未抛 ValueError" % bad)
        except ValueError:
            pass
    check("非法代码一律抛 ValueError（不静默映射）", ok)

    # —— history：真实 600519 报文 ——
    raw = open(os.path.join(root, FIXTURES,
                            "fflow_daykline_600519_2026-11-08.json"), encoding="utf-8").read()
    res = p.normalize(raw, mode="history", days=120)
    data, asof = res
    last = {k: v[-1] for k, v in data["flows"].items()}
    check("history 600519 行数 = %d" % EXPECT["history_600519_dates"],
          len(data["dates"]) == EXPECT["history_600519_dates"], "实际 %d" % len(data["dates"]))
    check("history 600519 末行资金与声明一致",
          all(abs(last[k] - v) < 1e-9 for k, v in EXPECT["history_600519_last"].items()),
          str(last))
    check("history asof 来自报文日期（2026-11-06）", asof == "2026-11-06", repr(asof))

    # —— 差分：Provider vs 迁移前实现（同一份报文）——
    klines = json.loads(raw)["data"]["klines"]
    d = diffs(legacy_history(klines, 120), data)
    check("history 差分（迁移前 vs Provider，120 行）差异项 = 0", not d, str(d[:3]))

    # —— history：第二只股票 ——
    raw2 = open(os.path.join(root, FIXTURES,
                             "fflow_daykline_000001_2026-11-08.json"), encoding="utf-8").read()
    data2, _ = p.normalize(raw2, mode="history", days=20)
    last2 = {k: v[-1] for k, v in data2["flows"].items()}
    check("history 000001 末行资金与声明一致",
          all(abs(last2[k] - v) < 1e-9 for k, v in EXPECT["history_000001_last"].items()),
          str(last2))

    # —— today ——
    raw3 = open(os.path.join(root, FIXTURES,
                             "fflow_kline_600519_2026-11-08.json"), encoding="utf-8").read()
    data3, asof3 = p.normalize(raw3, mode="today")
    amt = data3["amounts_yuan"]
    check("today 金额（元）与声明一致",
          all(abs(amt[k] - v) < 1e-6 for k, v in EXPECT["today_600519"].items()), str(amt))
    check("today 超大单 == 主力 − 大单",
          abs(amt["super_large"] - (amt["main_force"] - amt["large"])) < 1e-6)
    check("today asof 来自报文日期", asof3 == "2026-11-06", repr(asof3))

    # —— market_snapshot ——
    raw4 = open(os.path.join(root, FIXTURES,
                             "ulist_index_2026-11-08.json"), encoding="utf-8").read()
    data4, asof4 = p.normalize(raw4, mode="market_snapshot",
                               indices=({"name": "上证指数", "code": "000001"},
                                        {"name": "深证成指", "code": "399001"}))
    total = data4["total"]
    check("market_snapshot 合计与声明一致",
          all(abs(total[k] - v) < 1e-9 for k, v in EXPECT["snapshot_total"].items()), str(total))
    check("market_snapshot asof 为空（ulist 无时间戳，不造日期）", asof4 == "", repr(asof4))
    check("market_snapshot 未把占比字段(f69/f75)当金额",
          abs(total["main_force"] - EXPECT["snapshot_total"]["main_force"]) < 1e-9)
    check("market_snapshot 分指数名称按输入顺序",
          [r["name"] for r in data4["indices"]] == ["上证指数", "深证成指"])


# ------------------------------------------------------- 5. 测试日志核对
def verify_log(log_path):
    if not log_path:
        print("SKIP  测试日志（未提供 --log）")
        return
    if not os.path.exists(log_path):
        check("测试日志存在", False, log_path)
        return
    text = open(log_path, encoding="utf-8", errors="replace").read()
    m = re.search(r"^Ran (\d+) tests? in", text, re.M)
    ran = int(m.group(1)) if m else -1
    ok_count = len(re.findall(r"\.\.\. ok\s*$", text, re.M))
    has_ok = bool(re.search(r"^OK(\s|$)", text, re.M))
    bad = re.findall(r"^(?:FAIL|ERROR): ", text, re.M)
    check("日志里有 Ran 行", ran > 0, "未找到")
    check("日志结论为 OK（无 FAILED）", has_ok)
    check("日志内 FAIL/ERROR 条目 = 0", not bad, "有 %d 条" % len(bad))
    check("Ran 数（%d）与声明的 502 一致" % ran, ran == 502)
    # 有的用例会自己打印输出，'... ok' 可能被挤在同一行，因此用 <= 判定并说明
    check("逐条 ok 行数（%d）不超过 Ran 数" % ok_count, 0 < ok_count <= ran,
          "ok=%d ran=%d" % (ok_count, ran))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--manifest", default="")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    mode = install_requests_stub()
    print("== verify_package：只依赖标准库（requests: %s）" % mode)
    print("== 目标目录：%s\n" % os.path.abspath(args.root))

    verify_manifest(args.root, args.manifest)
    try:
        verify_parsing(args.root)
    except Exception as e:                                  # noqa: BLE001
        check("解析验证可执行", False, "%s: %s" % (type(e).__name__, e))
    verify_log(args.log)

    failed = [r for r in RESULTS if not r[1]]
    print("\n== 结果：%d 项检查，%d PASS，%d FAIL" %
          (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
