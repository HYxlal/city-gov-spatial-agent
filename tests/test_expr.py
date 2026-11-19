"""表达式求值与取值工具的测试。安全边界是重点：公式来自 LLM，属不可信输入。"""

from __future__ import annotations

import pytest

from src.argument.expr import (
    FormulaError,
    approx_equal,
    default_tolerance,
    get_path,
    safe_eval,
)


class TestSafeEval:
    def test_arithmetic(self) -> None:
        assert safe_eval("(count - base_count) / base_count * 100",
                         {"count": 45, "base_count": 40}) == pytest.approx(12.5)

    def test_unary_and_power(self) -> None:
        assert safe_eval("-a + b ** 2", {"a": 3, "b": 4}) == pytest.approx(13.0)

    def test_undefined_factor_raises(self) -> None:
        with pytest.raises(FormulaError, match="未声明的因子"):
            safe_eval("count / missing", {"count": 1})

    def test_rejects_import(self) -> None:
        """不得通过公式执行任意代码。"""
        with pytest.raises(FormulaError):
            safe_eval("__import__('os').system('echo hacked')", {})

    def test_rejects_attribute_access(self) -> None:
        with pytest.raises(FormulaError):
            safe_eval("(1).__class__", {})

    def test_rejects_string_constant(self) -> None:
        with pytest.raises(FormulaError):
            safe_eval("'abc'", {})

    def test_rejects_comparison(self) -> None:
        with pytest.raises(FormulaError):
            safe_eval("1 if count else 2", {"count": 1})

    def test_zero_division_raises(self) -> None:
        with pytest.raises(FormulaError, match="除零"):
            safe_eval("count / zero", {"count": 1, "zero": 0})


class TestGetPath:
    def test_nested_dict(self) -> None:
        assert get_path({"stats": {"count": 7}}, "stats.count") == 7

    def test_list_index(self) -> None:
        assert get_path({"stats": {"top": [10, 20]}}, "stats.top.1") == 20

    def test_missing_key(self) -> None:
        with pytest.raises(KeyError):
            get_path({"a": 1}, "b")

    def test_index_out_of_range(self) -> None:
        with pytest.raises(KeyError):
            get_path({"a": [1]}, "a.5")


class TestTolerance:
    def test_percentage_tolerance_is_tight_enough(self) -> None:
        """百分比字段不能沿用计数类的 0 容差，也不能放宽到 5% 绝对误差。"""
        abs_tol, rel_tol = default_tolerance("mom_pct", 12.5)
        assert abs_tol == pytest.approx(0.5)
        assert approx_equal(12.9, 12.5, abs_tol, rel_tol)
        assert not approx_equal(15.0, 12.5, abs_tol, rel_tol)

    def test_count_requires_exact(self) -> None:
        abs_tol, rel_tol = default_tolerance("count", 45)
        assert abs_tol == 0.0 and rel_tol == 0.0
        assert approx_equal(45, 45.0)
        assert not approx_equal(45, 46.0)
