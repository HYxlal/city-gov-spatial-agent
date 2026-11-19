"""What-if 复算与稳定性检查的测试。

执行器用假实现注入，不依赖数据库与地图服务，保证测试可离线复现。
"""

from __future__ import annotations

import pytest

from src.argument.digest import compute_digest
from src.argument.replay import check_stability, replay_operation
from src.argument.sag import Operation


def make_executor():
    """按统计天数返回不同结果的假执行器。"""

    def executor(tool_name: str, params: dict) -> dict:
        days = int(params.get("days", 7))
        if days <= 7:
            return {
                "count": 45,
                "top_ranking": ["g1", "g2", "g3"],
                "by_category": {"占道经营": 45},
            }
        return {
            "count": 70,
            "top_ranking": ["g2", "g1", "g3"],
            "by_category": {"占道经营": 60},
        }

    return executor


class TestReplay:
    def test_unchanged_params_identical(self, daily_operation: Operation) -> None:
        outcome = replay_operation(daily_operation, {"days": 7}, make_executor())
        assert outcome.identical
        assert not outcome.conclusion_flipped
        assert "完全一致" in outcome.summary

    def test_param_change_alters_digest(self, daily_operation: Operation) -> None:
        outcome = replay_operation(daily_operation, {"days": 14}, make_executor())
        assert not outcome.identical
        assert outcome.old_digest != outcome.new_digest

    def test_ranking_flip_detected(self, daily_operation: Operation) -> None:
        outcome = replay_operation(daily_operation, {"days": 14}, make_executor())
        assert outcome.conclusion_flipped
        assert outcome.rank_flips == ["top_ranking"]
        assert "排序字段" in outcome.summary

    def test_scalar_change_recorded(self, daily_operation: Operation) -> None:
        outcome = replay_operation(daily_operation, {"days": 14}, make_executor())
        assert "count" in outcome.scalar_changes
        assert outcome.scalar_changes["count"] == (45.0, 70.0)

    def test_undeclared_param_rejected(self, daily_operation: Operation) -> None:
        with pytest.raises(ValueError, match="未在 Operation.adjustable_params 中声明"):
            replay_operation(daily_operation, {"radius_m": 800}, make_executor())

    def test_out_of_range_param_rejected(self, daily_operation: Operation) -> None:
        with pytest.raises(ValueError, match="超出可调区间"):
            replay_operation(daily_operation, {"days": 90}, make_executor())

    def test_baseline_can_be_injected(self, daily_operation: Operation) -> None:
        outcome = replay_operation(
            daily_operation,
            {"days": 7},
            make_executor(),
            baseline={"count": 45, "top_ranking": ["g1", "g2", "g3"],
                      "by_category": {"占道经营": 45}},
        )
        assert outcome.identical


class TestStability:
    def test_deterministic_executor_is_consistent(self, daily_operation: Operation) -> None:
        report = check_stability(daily_operation, make_executor(), repeats=3)
        assert report.consistent
        assert len(set(report.digests)) == 1

    def test_flaky_executor_is_inconsistent(self, daily_operation: Operation) -> None:
        """真实场景里不稳定的来源是实时地图接口与缓存，不是 SQL 本身。"""
        counter = {"n": 0}

        def flaky(tool_name: str, params: dict) -> dict:
            counter["n"] += 1
            return {"count": 45, "path_cache": f"hit-{counter['n'] % 2}"}

        report = check_stability(daily_operation, flaky, repeats=3)
        assert not report.consistent
        assert report.unique_ratio > 0.5

    def test_repeats_must_be_meaningful(self, daily_operation: Operation) -> None:
        with pytest.raises(ValueError):
            check_stability(daily_operation, make_executor(), repeats=1)


class TestDigest:
    def test_key_order_independent(self) -> None:
        assert compute_digest({"a": 1, "b": 2}) == compute_digest({"b": 2, "a": 1})

    def test_value_change_detected(self) -> None:
        assert compute_digest({"a": 1}) != compute_digest({"a": 2})

    def test_nested_structure(self) -> None:
        left = compute_digest({"x": {"y": [1, 2, {"z": "中文"}]}})
        right = compute_digest({"x": {"y": [1, 2, {"z": "中文"}]}})
        assert left == right
        assert left.startswith("sha256:")
