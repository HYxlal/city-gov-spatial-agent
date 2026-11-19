"""安全表达式求值与数值比对工具。

派生数值的公式由 LLM 生成，属于不可信输入，绝不能用 ``eval``。
这里用 ``ast`` 白名单求值：只放行算术运算与因子名引用，其余语法一律拒绝。
"""

from __future__ import annotations

import ast
import operator
from typing import Any

_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class FormulaError(ValueError):
    """公式非法或求值失败。"""


def safe_eval(formula: str, factors: dict[str, float]) -> float:
    """在因子表约束下安全求值算术表达式。

    >>> safe_eval("(count - base_count) / base_count * 100", {"count": 42, "base_count": 37})
    13.513513513513514
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - 语法错误路径
        raise FormulaError(f"公式语法错误: {formula!r}") from exc
    return _eval_node(tree.body, factors, formula)


def _eval_node(node: ast.AST, factors: dict[str, float], formula: str) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError(f"公式只允许数值常量: {formula!r}")
        return float(node.value)

    if isinstance(node, ast.Name):
        if node.id not in factors:
            raise FormulaError(f"公式引用了未声明的因子 {node.id!r}: {formula!r}")
        return float(factors[node.id])

    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left, factors, formula)
        right = _eval_node(node.right, factors, formula)
        try:
            return float(_BIN_OPS[type(node.op)](left, right))
        except (ZeroDivisionError, OverflowError) as exc:
            raise FormulaError(f"公式求值溢出或除零: {formula!r}") from exc

    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return float(_UNARY_OPS[type(node.op)](_eval_node(node.operand, factors, formula)))

    raise FormulaError(f"公式含不允许的语法 {type(node).__name__}: {formula!r}")


def get_path(data: Any, path: str) -> Any:
    """按点路径取值，支持 dict 键与 list 下标。

    >>> get_path({"stats": {"by_cat": [10, 20]}}, "stats.by_cat.1")
    20
    """
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(f"字段不存在: {path!r}（断在 {part!r}）")
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise KeyError(f"字段不存在: {path!r}（下标 {part!r} 越界）") from exc
        else:
            raise KeyError(f"字段不存在: {path!r}（{type(current).__name__} 无子项）")
    return current


def default_tolerance(name: str, value: float) -> tuple[float, float]:
    """按指标语义给出缺省容差 (abs_tol, rel_tol)。

    推理依据：计数类必须精确；比率类允许 0.5 个百分点与 2% 相对误差；
    距离/面积类允许 1 个单位与 0.5% 相对误差。
    """
    lowered = name.lower()
    if any(k in lowered for k in ("pct", "percent", "ratio", "rate", "占比", "比例")):
        return 0.5, 0.02
    if any(k in lowered for k in ("count", "num", "total", "qty", "数量", "起数")):
        return 0.0, 0.0
    if any(k in lowered for k in ("dist", "meter", "radius", "area", "距离", "面积", "半径")):
        return 1.0, 0.005
    return 0.05, 0.005


def approx_equal(
    a: float,
    b: float,
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
) -> bool:
    """容忍性相等比较。两参数皆为零容差时退化为精确比较。"""
    scale = max(abs(a), abs(b))
    return abs(a - b) <= max(abs_tol, rel_tol * scale)
