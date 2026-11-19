"""Caveat（限定条件）的规则化生成。

关键设计：Caveat 必须由**证据与算子的客观属性**推导，不能由 LLM 自述。
否则它只是又一句提示词修辞，答辩时经不起"这些限定条件凭什么信"的追问。

生成侧与校验侧共用同一套规则（见 ``RULES``）：校验器会用同样的规则算出
"应有"的 Caveat 集合，与 Claim 实际携带的集合双向比对。
"""

from __future__ import annotations

from src.argument.sag import Caveat, Evidence, Operation, Provenance

#: 小样本阈值：样本量低于此值时必须提示，因为环比/占比在此规模下不稳定。
SMALL_SAMPLE_THRESHOLD = 5


def derive_caveats(
    evidences: list[Evidence],
    operation: Operation,
    provenance: Provenance,
) -> list[Caveat]:
    """按规则推导"应当存在"的 Caveat 列表（已按 kind+message 去重）。"""
    caveats: list[Caveat] = []

    # R1 算子降级：实际走了 fallback 链
    if operation.used_fallback():
        chain = " -> ".join(operation.fallback_chain)
        caveats.append(
            Caveat(
                kind="degradation",
                message=f"算子发生降级：{chain}，当前结果为降级路径产出",
                severity="warning",
                source_rule="R1_DEGRADATION",
            )
        )

    # R2 小样本：任一绑定证据样本量不足
    for evidence in evidences:
        if evidence.sample_size is not None and evidence.sample_size < SMALL_SAMPLE_THRESHOLD:
            caveats.append(
                Caveat(
                    kind="small_sample",
                    message=(
                        f"证据 {evidence.evidence_id} 样本量 {evidence.sample_size} "
                        f"< {SMALL_SAMPLE_THRESHOLD}，环比/占比波动可能较大"
                    ),
                    severity="info",
                    source_rule="R2_SMALL_SAMPLE",
                )
            )

    # R3 近似算法：算子声明了误差界
    if operation.error_bound_m is not None:
        caveats.append(
            Caveat(
                kind="approximation",
                message=f"算子为近似算法，误差界约 ±{operation.error_bound_m:g} m",
                severity="info",
                source_rule="R3_APPROXIMATION",
            )
        )

    # R4 参数触边界：可调参数已顶到区间端点，结论对该参数不再稳健
    for param in operation.adjustable_params:
        if param.at_boundary():
            caveats.append(
                Caveat(
                    kind="boundary",
                    message=(
                        f"参数 {param.name}={param.current_value:g}{param.unit} "
                        f"已处于可调区间边界 [{param.min_value:g}, {param.max_value:g}]"
                    ),
                    severity="info",
                    source_rule="R4_BOUNDARY",
                )
            )

    # R5 坐标基准：证据坐标系与展示基准不一致，需显式声明
    if provenance.srid.upper().replace("_", "-") not in ("GCJ-02", "GCJ02"):
        caveats.append(
            Caveat(
                kind="coordinate",
                message=(
                    f"数据源坐标系为 {provenance.srid}，与展示基准 GCJ-02 不一致，"
                    "已由适配层转换"
                ),
                severity="warning",
                source_rule="R5_COORDINATE",
            )
        )

    return _dedupe(caveats)


def _dedupe(caveats: list[Caveat]) -> list[Caveat]:
    seen: set[tuple[str, str]] = set()
    unique: list[Caveat] = []
    for caveat in caveats:
        key = (caveat.kind, caveat.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(caveat)
    return unique


def caveat_kinds(caveats: list[Caveat]) -> set[str]:
    return {c.kind for c in caveats}
