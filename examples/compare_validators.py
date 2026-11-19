"""对照实验：拍平式数值校验 vs 来源声明的分层校验。

用途有两个：
1. 证明"把 raw_metrics 里的数值拍平成集合再找成员"会误杀合法派生结论；
2. 作为论文消融实验的对照组脚本，输出可直接引用为表格。

运行::

    python examples/compare_validators.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 允许以 `python examples/compare_validators.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.argument.sag import Claim, Evidence, Metric, Operation, Provenance, SAG
from src.argument.validator import validate_sag

# ---- 被对照的两种实现 --------------------------------------------------------


def naive_flatten(data: Any) -> list[float]:
    """Gemini 方案的核心：递归拍平所有数值。"""
    numbers: list[float] = []
    if isinstance(data, dict):
        for value in data.values():
            numbers.extend(naive_flatten(value))
    elif isinstance(data, (list, tuple, set)):
        for item in data:
            numbers.extend(naive_flatten(item))
    elif isinstance(data, bool):
        pass
    elif isinstance(data, (int, float)):
        numbers.append(float(data))
    return numbers


def naive_check(claim: Claim, evidence: Evidence) -> tuple[bool, str]:
    """只判断 Claim 的数值是否在证据数值集合中出现过。"""
    pool = naive_flatten(evidence.raw_metrics)
    for metric in claim.metrics:
        if metric.source == "derived":
            # 原方案没有派生概念，只能拿声明值去集合里找
            target = metric.value
        else:
            target = metric.value
        hit = any(abs(target - n) <= max(0.05, 0.01 * max(abs(target), abs(n))) for n in pool)
        if not hit:
            return False, f"数值 {target:g} 不在证据中"
    return True, "通过"


# ---- 实验数据 -----------------------------------------------------------------

H3 = "88309a4123fffff"

PROVENANCE = Provenance(
    provenance_id="prov_events",
    source_name="fact_event",
    time_window="2026-11-12 ~ 2026-11-18",
)

OPERATION = Operation(
    operation_id="op_events",
    tool_name="query_events",
    template_or_sql="SELECT h3_idx, count(*) FROM fact_event GROUP BY h3_idx",
    params={"category": "占道经营", "days": 7},
)

BASE_EVIDENCE = Evidence(
    evidence_id="ev_hotspot",
    kind="grid_agg",
    summary="网格 88309a4123fffff 本周 45 起，上周 40 起",
    raw_metrics={"h3_idx": H3, "count": 45, "base_count": 40},
    operation=OPERATION,
    provenance=PROVENANCE,
    sample_size=45,
)


def build_cases() -> list[tuple[str, Claim, Evidence, str]]:
    """返回 (场景, claim, evidence, 人工判定)。"""
    derived_metric = Metric(
        name="mom_pct", value=12.5, source="derived",
        formula="(count - base_count) / base_count * 100",
        factors={"count": 45, "base_count": 40},
    )

    # 场景 D 的证据里直接带 mom_pct，模拟接口已返回环比字段
    evidence_with_pct = BASE_EVIDENCE.model_copy(
        update={"raw_metrics": {**BASE_EVIDENCE.raw_metrics, "mom_pct": 12.5}}
    )

    return [
        (
            "A 原始字段值",
            Claim(
                claim_id="c_a", text="网格 88309a4123fffff 本周 45 起。",
                metrics=[Metric(name="count", value=45, source="raw", field="count")],
                evidence_ids=["ev_hotspot"],
            ),
            BASE_EVIDENCE, "应通过",
        ),
        (
            "B 派生百分比",
            Claim(
                claim_id="c_b", text="网格 88309a4123fffff 环比上升 12.5%。",
                metrics=[derived_metric], evidence_ids=["ev_hotspot"],
            ),
            BASE_EVIDENCE, "应通过",
        ),
        (
            "C 编造数值",
            Claim(
                claim_id="c_c", text="网格 88309a4123fffff 本周 12 起。",
                metrics=[Metric(name="count", value=12, source="raw", field="count")],
                evidence_ids=["ev_hotspot"],
            ),
            BASE_EVIDENCE, "应拒绝",
        ),
        (
            "D 数值对但说反",
            Claim(
                claim_id="c_d", text="网格 88309a4123fffff 环比显著下降。",
                metrics=[
                    Metric(name="mom_pct", value=12.5, source="raw", field="mom_pct")
                ],
                evidence_ids=["ev_hotspot"],
            ),
            evidence_with_pct, "应拒绝",
        ),
        (
            "E 派生因子编造",
            Claim(
                claim_id="c_e", text="网格 88309a4123fffff 环比上升 33.3%。",
                metrics=[
                    Metric(
                        name="mom_pct", value=33.3, source="derived",
                        formula="(count - base_count) / base_count * 100",
                        factors={"count": 45, "base_count": 33.75},
                    )
                ],
                evidence_ids=["ev_hotspot"],
            ),
            BASE_EVIDENCE, "应拒绝",
        ),
    ]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cases = build_cases()
    rows: list[tuple[str, str, str, str, str]] = []
    naive_wrong = sag_wrong = 0

    for name, claim, evidence, expected in cases:
        sag = SAG(
            sag_id="sag_compare", thread_id="t", user_query="对照实验",
            claims=[claim], evidences={evidence.evidence_id: evidence},
        )
        sag_report = validate_sag(sag, check_caveats=False)
        sag_verdict = "通过" if sag_report.passed else "拒绝"

        naive_ok, naive_reason = naive_check(claim, evidence)
        naive_verdict = "通过" if naive_ok else "拒绝"

        naive_mark = "✓" if f"应{naive_verdict}" == expected else "✗"
        sag_mark = "✓" if f"应{sag_verdict}" == expected else "✗"
        naive_wrong += naive_mark == "✗"
        sag_wrong += sag_mark == "✗"

        reason = ""
        if sag_report.issues:
            reason = sag_report.issues[0].code
        elif not naive_ok:
            reason = naive_reason
        rows.append((name, expected, f"{naive_verdict} {naive_mark}", f"{sag_verdict} {sag_mark}", reason))

    width = max(len(r[0]) for r in rows)
    print(f"{'场景':<{width}}  {'人工判定':<8} {'拍平式校验':<12} {'本实现':<12} 说明")
    print("-" * (width + 60))
    for name, expected, naive, sag_v, reason in rows:
        print(f"{name:<{width}}  {expected:<8} {naive:<12} {sag_v:<12} {reason}")

    print()
    print(f"拍平式校验误判: {naive_wrong}/{len(cases)}    本实现误判: {sag_wrong}/{len(cases)}")
    return 0 if sag_wrong == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
