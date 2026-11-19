"""测试夹具：一条完整的 S1 场景证据链。

数据设定刻意让派生值整除：
    网格 88309a4123fffff，本周占道经营 45 起，上周 40 起
    环比 = (45 - 40) / 40 * 100 = 12.5%
这样"结论说 12.5%"与"证据里没有 12.5 这个数"同时成立，正好用来验证
派生数值是否被正确校验——这正是把数值拍平成集合再找成员的实现会误杀的场景。
"""

from __future__ import annotations

import pytest

from src.argument.sag import (
    AdjustableParam,
    Claim,
    Evidence,
    Metric,
    Operation,
    Provenance,
    SAG,
)

H3_ID = "88309a4123fffff"


@pytest.fixture
def daily_operation() -> Operation:
    return Operation(
        operation_id="op_events_7d",
        tool_name="query_events",
        template_or_sql=(
            "SELECT h3_idx, count(*) AS count FROM fact_event "
            "WHERE category = %(category)s AND ts >= now() - interval '%(days)s days' "
            "GROUP BY h3_idx ORDER BY count DESC LIMIT %(topk)s"
        ),
        params={"category": "占道经营", "days": 7, "topk": 5},
        adjustable_params=[
            AdjustableParam(
                name="days", label="统计天数", current_value=7,
                min_value=3, max_value=30, step=1, unit="天",
            ),
            AdjustableParam(
                name="topk", label="返回网格数", current_value=5,
                min_value=3, max_value=20, step=1, unit="个",
            ),
        ],
    )


@pytest.fixture
def gcj_provenance() -> Provenance:
    return Provenance(
        provenance_id="prov_fact_event_v1",
        source_name="fact_event",
        time_window="2026-11-12 ~ 2026-11-18",
        srid="GCJ-02",
        data_version="synthetic-seed-42",
    )


@pytest.fixture
def evidence(daily_operation: Operation, gcj_provenance: Provenance) -> Evidence:
    return Evidence(
        evidence_id="ev_hotspot",
        kind="grid_agg",
        summary=f"网格 {H3_ID} 本周占道经营 45 起，上周 40 起",
        raw_metrics={
            "h3_idx": H3_ID,
            "count": 45,
            "base_count": 40,
            "top_ranking": [H3_ID, "88309a4125fffff", "88309a4127fffff"],
        },
        layer_id="layer_hotspot_top5",
        operation=daily_operation,
        provenance=gcj_provenance,
        sample_size=45,
    )


@pytest.fixture
def small_sample_evidence(evidence: Evidence) -> Evidence:
    """样本量不足的同构证据，用于触发 small_sample 规则。"""
    return evidence.model_copy(
        update={
            "evidence_id": "ev_small",
            "raw_metrics": {"h3_idx": "88309a4129fffff", "count": 2, "base_count": 1},
            "sample_size": 2,
        }
    )


@pytest.fixture
def claim(evidence: Evidence) -> Claim:
    """一条正常结论：数值正确、实体可追溯、无方向冲突。"""
    return Claim(
        claim_id="c_normal",
        text=f"网格 {H3_ID} 本周占道经营 45 起，环比上升 12.5%。",
        metrics=[
            Metric(name="count", value=45, source="raw", field="count"),
            Metric(
                name="mom_pct",
                value=12.5,
                source="derived",
                formula="(count - base_count) / base_count * 100",
                factors={"count": 45, "base_count": 40},
            ),
        ],
        extracted_entities=[H3_ID],
        evidence_ids=[evidence.evidence_id],
    )


@pytest.fixture
def sag_factory(evidence: Evidence):
    """按需组装 SAG，避免每个用例重复写样板。"""

    def _make(claims: list[Claim], evidences: list[Evidence] | None = None) -> SAG:
        pool = evidences if evidences is not None else [evidence]
        return SAG(
            sag_id="sag_test",
            thread_id="thread_test",
            user_query="统计上周占道经营最集中的网格及环比变化",
            claims=claims,
            evidences={e.evidence_id: e for e in pool},
        )

    return _make
