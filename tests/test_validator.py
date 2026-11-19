"""SAG 出口校验的行为测试。

用例按校验层级组织，重点是三类：
- 结构层必须拦住"没有证据的结论"；
- 绑定层必须放过派生数值、拦住虚假绑定与无出处因子；
- 语义层必须抓住"数字对但说反了"，同时不误杀正常表述。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.argument.sag import Claim, Evidence, Metric, Operation
from src.argument.validator import (
    E_DIRECTION_CONFLICT,
    E_ENTITY_UNTRACED,
    E_EVIDENCE_MISSING,
    E_METRIC_MISMATCH,
    E_NO_EVIDENCE,
    E_OPERATION_INVALID,
    W_CAVEAT_MISSING,
    W_FUZZY_METRIC,
    validate_sag,
)


def _codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


class TestStructureLayer:
    def test_empty_evidence_ids_rejected_by_model(self) -> None:
        """no-evidence-no-claim 由模型强制，构造阶段就失败。"""
        with pytest.raises(ValidationError):
            Claim(claim_id="c_bad", text="无证据的结论", evidence_ids=[])

    def test_missing_evidence(self, sag_factory, evidence) -> None:
        claim = Claim(
            claim_id="c_missing",
            text="网格 88309a4123fffff 本周 45 起。",
            metrics=[Metric(name="count", value=45, source="raw", field="count")],
            evidence_ids=["ev_not_exist"],
        )
        report = validate_sag(sag_factory([claim]))
        assert not report.passed
        assert E_EVIDENCE_MISSING in _codes(report)
        assert report.needs_replan

    def test_non_executable_operation(self, sag_factory, evidence) -> None:
        hollow = evidence.model_copy(
            update={
                "operation": Operation(
                    operation_id="op_empty", tool_name="", template_or_sql=""
                )
            }
        )
        claim = Claim(
            claim_id="c_hollow",
            text="网格 88309a4123fffff 本周 45 起。",
            metrics=[Metric(name="count", value=45, source="raw", field="count")],
            evidence_ids=[hollow.evidence_id],
        )
        report = validate_sag(sag_factory([claim], [hollow]))
        assert E_OPERATION_INVALID in _codes(report)

    def test_empty_sag(self) -> None:
        from src.argument.sag import SAG

        report = validate_sag(SAG(sag_id="s", thread_id="t", user_query="q"))
        assert not report.passed
        assert E_NO_EVIDENCE in _codes(report)


class TestBindingLayer:
    def test_normal_claim_passes(self, sag_factory, claim) -> None:
        report = validate_sag(sag_factory([claim]))
        assert report.passed, report.issues
        assert report.verified_claim_ids == ["c_normal"]

    def test_fabricated_number_blocked(self, sag_factory, evidence) -> None:
        """挂着合法证据把 45 编成 12，必须拦住。"""
        claim = Claim(
            claim_id="c_fake_count",
            text="网格 88309a4123fffff 本周占道经营 12 起。",
            metrics=[Metric(name="count", value=12, source="raw", field="count")],
            evidence_ids=[evidence.evidence_id],
        )
        report = validate_sag(sag_factory([claim]))
        assert not report.passed
        assert E_METRIC_MISMATCH in _codes(report)
        assert "实际值为 45" in " ".join(i.message for i in report.issues)

    def test_derived_percentage_passes(self, sag_factory, claim) -> None:
        """回归测试：12.5 不在证据原始数值里，但它是合法派生值，不能拦。

        把数值拍平成集合再找成员的实现会在这里误杀正确结论。
        """
        report = validate_sag(sag_factory([claim]))
        assert report.passed, [i.message for i in report.issues]
        derived_ok = any(
            "派生复算一致" in issue.message for issue in report.issues
        ) or report.passed
        assert derived_ok

    def test_derived_miscalculation_blocked(self, sag_factory, evidence) -> None:
        claim = Claim(
            claim_id="c_bad_derived",
            text="网格 88309a4123fffff 环比上升 15.0%。",
            metrics=[
                Metric(
                    name="mom_pct", value=15.0, source="derived",
                    formula="(count - base_count) / base_count * 100",
                    factors={"count": 45, "base_count": 40},
                )
            ],
            evidence_ids=[evidence.evidence_id],
        )
        report = validate_sag(sag_factory([claim]))
        assert not report.passed
        assert E_METRIC_MISMATCH in _codes(report)
        assert "派生复算不一致" in " ".join(i.message for i in report.issues)

    def test_derived_factor_without_provenance_blocked(self, sag_factory, evidence) -> None:
        """允许派生但不校验因子，等于给模型开了个更大的后门。"""
        claim = Claim(
            claim_id="c_ghost_factor",
            text="网格 88309a4123fffff 环比上升 33.3%。",
            metrics=[
                Metric(
                    name="mom_pct", value=33.3, source="derived",
                    formula="(count - base_count) / base_count * 100",
                    # base_count 被改成 33.75，证据里没有这个数
                    factors={"count": 45, "base_count": 33.75},
                )
            ],
            evidence_ids=[evidence.evidence_id],
        )
        report = validate_sag(sag_factory([claim]))
        assert not report.passed
        assert "无出处" in " ".join(i.message for i in report.issues)

    def test_nested_field_path(self, sag_factory, evidence) -> None:
        nested = evidence.model_copy(
            update={
                "raw_metrics": {
                    **evidence.raw_metrics,
                    "stats": {"by_category": {"占道经营": 45}},
                }
            }
        )
        claim = Claim(
            claim_id="c_nested",
            text="占道经营 45 起。",
            metrics=[
                Metric(
                    name="count", value=45, source="raw",
                    field="stats.by_category.占道经营",
                )
            ],
            evidence_ids=[nested.evidence_id],
        )
        report = validate_sag(sag_factory([claim], [nested]))
        assert report.passed, [i.message for i in report.issues]

    def test_entity_not_traced(self, sag_factory, evidence) -> None:
        claim = Claim(
            claim_id="c_ghost_entity",
            text="网格 88309a4123fffff 与朝阳区相关。",
            metrics=[Metric(name="count", value=45, source="raw", field="count")],
            extracted_entities=["朝阳区"],
            evidence_ids=[evidence.evidence_id],
        )
        report = validate_sag(sag_factory([claim]))
        assert E_ENTITY_UNTRACED in _codes(report)

    def test_entity_alias_matches(self, sag_factory, evidence) -> None:
        """'朝阳区' 应能匹配证据里的 '朝阳'，不该因后缀差异误杀。"""
        aliased = evidence.model_copy(
            update={"raw_metrics": {**evidence.raw_metrics, "district": "朝阳"}}
        )
        claim = Claim(
            claim_id="c_alias",
            text="朝阳区网格 88309a4123fffff 本周 45 起。",
            metrics=[Metric(name="count", value=45, source="raw", field="count")],
            extracted_entities=["朝阳区", "88309a4123fffff"],
            evidence_ids=[aliased.evidence_id],
        )
        report = validate_sag(sag_factory([claim], [aliased]))
        assert E_ENTITY_UNTRACED not in _codes(report)

    def test_simplified_numbers_fall_back_to_fuzzy(self, sag_factory, evidence) -> None:
        """简化写法 {'count': 45} 能解析则通过；无法归因时记警告而非直接拒绝。"""
        claim = Claim(
            claim_id="c_simplified",
            text="本周 45 起。",
            extracted_entities=[],
            extracted_numbers={"count": 45},
            evidence_ids=[evidence.evidence_id],
        )
        report = validate_sag(sag_factory([claim]))
        assert report.passed, [i.message for i in report.issues]

    def test_lenient_mode_downgrades_binding_errors(self, sag_factory, evidence) -> None:
        claim = Claim(
            claim_id="c_lenient",
            text="本周 12 起。",
            metrics=[Metric(name="count", value=12, source="raw", field="count")],
            evidence_ids=[evidence.evidence_id],
        )
        strict = validate_sag(sag_factory([claim]), level="strict")
        assert not strict.passed

        claim_again = claim.model_copy(update={"verified": False})
        lenient = validate_sag(
            sag_factory([claim_again]), level="lenient", check_caveats=False
        )
        assert lenient.passed
        assert all(i.severity == "warning" for i in lenient.issues)


class TestSemanticLayer:
    def test_direction_conflict_blocked(self, sag_factory, evidence) -> None:
        """数值本身能对上，但文字说反了 —— 这是拍平式校验抓不到的幻觉。"""
        claim = Claim(
            claim_id="c_wrong_direction",
            text="网格 88309a4123fffff 占道经营环比显著下降。",
            metrics=[
                Metric(
                    name="mom_pct", value=12.5, source="derived",
                    formula="(count - base_count) / base_count * 100",
                    factors={"count": 45, "base_count": 40},
                )
            ],
            evidence_ids=[evidence.evidence_id],
        )
        report = validate_sag(sag_factory([claim]))
        assert E_DIRECTION_CONFLICT in _codes(report)
        assert not report.passed

    def test_direction_does_not_misfire_on_absolute_value(self, sag_factory, evidence) -> None:
        """'投诉量下降到 45 起' 中的 45 是绝对值，不是变化量，不能判为方向冲突。"""
        claim = Claim(
            claim_id="c_absolute",
            text="网格 88309a4123fffff 投诉量下降到 45 起。",
            metrics=[Metric(name="count", value=45, source="raw", field="count")],
            evidence_ids=[evidence.evidence_id],
        )
        report = validate_sag(sag_factory([claim]))
        assert E_DIRECTION_CONFLICT not in _codes(report)
        assert report.passed

    def test_mixed_direction_text_skipped(self, sag_factory, evidence) -> None:
        claim = Claim(
            claim_id="c_mixed",
            text="占道经营环比上升 12.5%，但投诉总量下降。",
            metrics=[
                Metric(
                    name="mom_pct", value=12.5, source="derived",
                    formula="(count - base_count) / base_count * 100",
                    factors={"count": 45, "base_count": 40},
                )
            ],
            evidence_ids=[evidence.evidence_id],
        )
        report = validate_sag(sag_factory([claim]))
        assert E_DIRECTION_CONFLICT not in _codes(report)


class TestReport:
    def test_stats_counts_by_code(self, sag_factory, evidence) -> None:
        bad = Claim(
            claim_id="c_bad",
            text="本周 12 起。",
            metrics=[Metric(name="count", value=12, source="raw", field="count")],
            evidence_ids=[evidence.evidence_id],
        )
        report = validate_sag(sag_factory([bad]))
        stats = report.stats()
        assert stats["verified"] == 0
        assert stats[E_METRIC_MISMATCH] >= 1

    def test_rejected_claims_recorded(self, sag_factory, evidence) -> None:
        sag = sag_factory(
            [
                Claim(
                    claim_id="c_bad",
                    text="本周 12 起。",
                    metrics=[Metric(name="count", value=12, source="raw", field="count")],
                    evidence_ids=[evidence.evidence_id],
                )
            ]
        )
        validate_sag(sag)
        assert sag.rejected_claims
        assert sag.rejected_claims[0].startswith("c_bad:")

    def test_caveat_missing_warning(self, sag_factory, small_sample_evidence) -> None:
        claim = Claim(
            claim_id="c_small",
            text="网格 88309a4129fffff 本周 2 起，环比上升 100%。",
            metrics=[
                Metric(name="count", value=2, source="raw", field="count"),
                Metric(
                    name="mom_pct", value=100.0, source="derived",
                    formula="(count - base_count) / base_count * 100",
                    factors={"count": 2, "base_count": 1},
                ),
            ],
            evidence_ids=[small_sample_evidence.evidence_id],
        )
        report = validate_sag(sag_factory([claim], [small_sample_evidence]))
        assert W_CAVEAT_MISSING in _codes(report)
