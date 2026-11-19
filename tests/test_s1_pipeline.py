"""S1 端到端链路的集成测试。

跑的是完整的"数据 → 算子 → 证据 → 结论 → 校验 → What-if"流程，
断言三类结论分别被放行/拦截。这条链路如果断了，演示就没了。
"""

from __future__ import annotations

import contextlib
import importlib.util
from io import StringIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "s1_demo", ROOT / "examples" / "s1_end_to_end.py"
)
assert _spec and _spec.loader
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


@pytest.fixture(scope="module")
def output() -> str:
    buffer = StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = demo.main()
    assert exit_code == 0
    return buffer.getvalue()


def test_good_claim_passes(output: str) -> None:
    rows = [line for line in output.splitlines() if line.strip().startswith("c_good")]
    assert rows, output
    assert "通过" in rows[0]


def test_wrong_direction_blocked(output: str) -> None:
    assert "E_DIRECTION_CONFLICT" in output


def test_fabricated_number_blocked(output: str) -> None:
    assert "E_METRIC_MISMATCH" in output


def test_caveat_auto_generated(output: str) -> None:
    """资源算子是环形近似，必须自动生成 approximation 限定条件。"""
    assert "approximation" in output


def test_whatif_detects_flip(output: str) -> None:
    assert "结论翻转: True" in output
    assert "top_ranking" in output


def test_operators_are_deterministic(output: str) -> None:
    """同一份数据跑两次，输出应完全一致（数据与算子都无随机性）。"""
    buffer = StringIO()
    with contextlib.redirect_stdout(buffer):
        demo.main()
    assert buffer.getvalue() == output
