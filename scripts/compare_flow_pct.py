# -*- coding: utf-8 -*-
"""S3-2 证据脚本：用**真实腾讯夹具**里的成交额，对比迁移前后五种资金占比。

旧公式（S3-1 末态）：round(v / (max(turnover, 0.01) * 1e8) * 100, 2)
新公式（S3-2）：     round(v / (turnover * 1e8) * 100, 2)      # 分母为真实成交额

用法：python scripts/compare_flow_pct.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.data.providers.tencent import TencentQuoteProvider  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "s3_0", "tencent_quote_sh600519.raw")
PCT_FIELDS = ("main_force_pct", "super_large_pct", "large_pct", "middle_pct", "small_pct")
AMOUNTS = {                      # 单位：元（示意用的真实量级）
    "main_force": 1e8,
    "super_large": 6e7,
    "large": 4e7,
    "middle": -2e7,
    "small": -1e7,
}


def old_pct(v, turnover_yi):
    if turnover_yi is None or turnover_yi <= 0:
        return None
    return round(v / (max(turnover_yi, 0.01) * 1e8) * 100, 2)


def new_pct(v, turnover_yi):
    if turnover_yi is None or not isinstance(turnover_yi, (int, float)) or turnover_yi <= 0:
        return None
    return round(v / (turnover_yi * 1e8) * 100, 2)


def main():
    with open(FIXTURE, "rb") as f:
        raw = f.read().decode("utf-8", errors="replace")
    parts, _asof = TencentQuoteProvider().normalize(raw, code="600519")
    quote = TencentQuoteProvider().parse_quote(parts, "600519")
    turnover = quote["turnover"]

    print("== 真实夹具 ==")
    print(f"文件：tests/fixtures/s3_0/tencent_quote_sh600519.raw"
          f"（{os.path.getsize(FIXTURE)} 字节 / 解码后 {len(raw)} 字符）")
    print(f"parts[57] 原始值（万元）：{parts[57]!r}")
    print(f"Provider 对外 turnover（亿元）：{turnover}")
    print()

    print("== 正常成交额：新旧公式逐项对照 ==")
    print(f"{'字段':<18}{'金额(元)':>14}{'旧公式 %':>12}{'新公式 %':>12}{'差异':>8}")
    diffs = 0
    for f in PCT_FIELDS:
        amount = AMOUNTS[f.replace("_pct", "")]
        o, n = old_pct(amount, turnover), new_pct(amount, turnover)
        diffs += 0 if o == n else 1
        print(f"{f:<18}{amount:>14.0f}{str(o):>12}{str(n):>12}{'一致' if o == n else '差异':>8}")
    print(f"\n差异项：{diffs}")
    print()

    print("== 边界：分母钳制被删除前后 ==")
    print(f"{'成交额(亿)':>12}{'旧分母':>10}{'新分母':>10}{'旧 main_force_pct':>20}{'新 main_force_pct':>20}")
    for t in (0.0049, 0.005, 0.0099, 0.01, 0.02, 0.5, 33.16):
        old_d = max(t, 0.01) if t > 0 else None
        print(f"{t:>12.4f}{str(old_d):>10}{str(t):>10}"
              f"{str(old_pct(1e8, t)):>20}{str(new_pct(1e8, t)):>20}")
    print()

    print("== 缺失 / 零 / 负数 ==")
    for t in (None, 0, 0.0, -3.2):
        print(f"turnover={str(t):>6} -> 旧 {old_pct(1e8, t)!s:>5} / 新 {new_pct(1e8, t)!s:>5}")
    print()

    ok = diffs == 0 and new_pct(1e8, 0.005) == 20000.0 and new_pct(0, 0.0) is None
    print("结论：" + ("正常成交额零差异，钳制已删除，缺失/零/负数均不计算" if ok else "存在异常，见上表"))
    print(json.dumps({"turnover_yi": turnover, "diffs": diffs}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
