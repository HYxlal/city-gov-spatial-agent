"""Caveat 规则化生成的测试。

核心主张：限定条件必须由证据与算子的客观属性推导，不能由模型自述。
所以两侧都要测——该生成而没生成，与不该生成却编了一个。
"""

from __future__ import annotations

from src.argument.caveat import derive_caveats
from src.argument.sag import Caveat, Claim, Metric, Operation, Provenance
from src.argument.validator import W_CAVEAT_UNSUPPORTED, validate_sag


def _kinds(caveats) -> set[str]:
    return {c.kind for c in caveats}


class TestDerivation:
    def test_clean_evidence_has_no_caveat(self, evidence) -> None:
        caveats = derive_caveats([evidence], evidence.operation, evidence.provenance)
        assert caveats == []

    def test_small_sample(self, small_sample_evidence) -> None:
        caveats = derive_caveats(
            [small_sample_evidence],
            small_sample_evidence.operation,
            small_sample_evidence.provenance,
        )
        assert "small_sample" in _kinds(caveats)
        assert all(c.auto_generated for c in caveats)

    def test_degradation_only_when_fallback_used(self, evidence) -> None:
        op = evidence.operation.model_copy(
            update={"fallback_chain": ["amap_isochrone", "postgis_buffer"]}
        )
        assert derive_caveats([evidence], op, evidence.provenance) == []

        degraded = op.model_copy(update={"params": {**op.params, "_used_fallback": True}})
        caveats = derive_caveats([evidence], degraded, evidence.provenance)
        assert "degradation" in _kinds(caveats)
        assert "amap_isochrone -> postgis_buffer" in caveats[0].message

    def test_approximation_error_bound(self, evidence) -> None:
        op = evidence.operation.model_copy(update={"error_bound_m": 120.0})
        caveats = derive_caveats([evidence], op, evidence.provenance)
        assert "approximation" in _kinds(caveats)

    def test_boundary_param(self, evidence) -> None:
        op = evidence.operation.model_copy(
            update={
                "adjustable_params": [
                    p.model_copy(update={"current_value": p.max_value})
                    for p in evidence.operation.adjustable_params
                ]
            }
        )
        caveats = derive_caveats([evidence], op, evidence.provenance)
        assert "boundary" in _kinds(caveats)

    def test_coordinate_basis_mismatch(self, evidence) -> None:
        prov = Provenance(
            provenance_id="prov_gps",
            source_name="gps_track",
            srid="WGS-84",
        )
        caveats = derive_caveats([evidence], evidence.operation, prov)
        assert "coordinate" in _kinds(caveats)

    def test_dedupe(self, evidence) -> None:
        op = evidence.operation.model_copy(update={"error_bound_m": 50.0})
        caveats = derive_caveats([evidence, evidence], op, evidence.provenance)
        assert len(caveats) == len({(c.kind, c.message) for c in caveats})


class TestValidation:
    def test_declared_caveat_satisfies_rule(self, sag_factory, small_sample_evidence) -> None:
        claim = Claim(
            claim_id="c_with_caveat",
            text="网格 88309a4129fffff 本周 2 起。",
            metrics=[Metric(name="count", value=2, source="raw", field="count")],
            evidence_ids=[small_sample_evidence.evidence_id],
            caveats=[
                Caveat(
                    kind="small_sample",
                    message="样本量 2 < 5",
                    severity="info",
                    source_rule="R2_SMALL_SAMPLE",
                )
            ],
        )
        report = validate_sag(
            sag_factory([claim], [small_sample_evidence]), check_caveats=True
        )
        assert report.passed, [i.message for i in report.issues]
        assert not any(i.code.startswith("W_CAVEAT_MISSING") for i in report.issues)

    def test_fabricated_caveat_flagged(self, sag_factory, evidence) -> None:
        """规则不支撑的限定条件是编造，同样要报出来。"""
        claim = Claim(
            claim_id="c_fake_caveat",
            text="网格 88309a4123fffff 本周 45 起。",
            metrics=[Metric(name="count", value=45, source="raw", field="count")],
            evidence_ids=[evidence.evidence_id],
            caveats=[
                Caveat(
                    kind="degradation",
                    message="高德等时圈已降级为缓冲区近似",
                    auto_generated=False,
                )
            ],
        )
        report = validate_sag(sag_factory([claim]))
        assert W_CAVEAT_UNSUPPORTED in {i.code for i in report.issues}
        # 只是警告，结论本身数值无问题，仍应通过
        assert report.passed
