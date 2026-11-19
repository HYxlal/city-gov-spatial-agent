"""What-if 参数敏感性复算。

要区分两个被混为一谈的概念：

- **复算一致性**（``check_stability``）：同一 Operation、同一参数重复执行，
  结果 digest 是否稳定。在纯静态数据 + 确定性 SQL 上必然稳定，所以这个
  指标的价值不在证明"SQL 跑两遍一样"，而在于暴露 LLM 结论改写、实时地图
  接口抖动、缓存命中差异带来的不稳定。
- **参数敏感性**（``replay_operation``）：改动空间参数后结论是否翻转。
  这是稳健性指标，与一致性不是同一件事，论文中须分别命名。

执行器通过参数注入，测试时可用假实现，生产时接工具层。
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from src.argument.digest import compute_digest
from src.argument.sag import Operation

#: 执行器签名：接收工具名与参数，返回结构化结果 dict。
Executor = Callable[[str, dict[str, Any]], dict[str, Any]]

#: 标量相对变化超过该比例即视为结论发生实质变化。
FLIP_THRESHOLD = 0.2


class ReplayOutcome(BaseModel):
    """一次 What-if 复算的对比结果。"""

    operation_id: str
    param_changes: dict[str, Any] = Field(default_factory=dict)
    old_digest: str
    new_digest: str
    identical: bool
    scalar_changes: dict[str, tuple[float, float]] = Field(default_factory=dict)
    rank_flips: list[str] = Field(default_factory=list)
    conclusion_flipped: bool = False
    summary: str = ""


class StabilityReport(BaseModel):
    """重复执行的一致性报告。"""

    operation_id: str
    repeats: int
    digests: list[str]
    consistent: bool

    @property
    def unique_ratio(self) -> float:
        return len(set(self.digests)) / max(len(self.digests), 1)


def replay_operation(
    operation: Operation,
    overrides: dict[str, float | int],
    executor: Executor,
    *,
    baseline: dict[str, Any] | None = None,
    flip_threshold: float = FLIP_THRESHOLD,
) -> ReplayOutcome:
    """按新参数重跑同一算子，并与基线结果比对。

    Args:
        operation: 原始算子定义（含 params 与 adjustable_params）。
        overrides: 参数覆盖，如 ``{"radius_m": 800}``。必须在可调声明内。
        executor: 工具执行器。
        baseline: 基线结果。省略时按原参数先跑一次。
        flip_threshold: 标量变化达到该比例即判定结论翻转。
    """
    _assert_overrides_allowed(operation, overrides)

    if baseline is None:
        baseline = executor(operation.tool_name, dict(operation.params))

    new_params = {**operation.params, **overrides}
    replayed = executor(operation.tool_name, new_params)

    base_digest = compute_digest(baseline)
    new_digest = compute_digest(replayed)
    scalar_changes, rank_flips = _diff(baseline, replayed)

    flipped = bool(rank_flips) or any(
        _relative_change(old, new) >= flip_threshold for old, new in scalar_changes.values()
    )

    return ReplayOutcome(
        operation_id=operation.operation_id,
        param_changes=dict(overrides),
        old_digest=base_digest,
        new_digest=new_digest,
        identical=base_digest == new_digest,
        scalar_changes=scalar_changes,
        rank_flips=rank_flips,
        conclusion_flipped=flipped,
        summary=_summarize(overrides, scalar_changes, rank_flips, flipped),
    )


def check_stability(
    operation: Operation,
    executor: Executor,
    *,
    repeats: int = 3,
) -> StabilityReport:
    """重复执行同一算子，检查结果 digest 是否稳定。"""
    if repeats < 2:
        raise ValueError("repeats 至少为 2，否则无法检验一致性")

    digests = [
        compute_digest(executor(operation.tool_name, dict(operation.params)))
        for _ in range(repeats)
    ]
    return StabilityReport(
        operation_id=operation.operation_id,
        repeats=repeats,
        digests=digests,
        consistent=len(set(digests)) == 1,
    )


# ---- 内部实现 ----------------------------------------------------------------


def _assert_overrides_allowed(operation: Operation, overrides: dict[str, Any]) -> None:
    """参数覆盖必须落在声明过的可调参数及其取值范围内。"""
    declared = {p.name: p for p in operation.adjustable_params}
    for name, value in overrides.items():
        if name not in declared:
            raise ValueError(
                f"参数 {name!r} 未在 Operation.adjustable_params 中声明，"
                f"可调参数为 {sorted(declared)}"
            )
        param = declared[name]
        if not param.min_value <= value <= param.max_value:
            raise ValueError(
                f"参数 {name}={value} 超出可调区间 "
                f"[{param.min_value}, {param.max_value}]"
            )


def _diff(
    before: dict[str, Any],
    after: dict[str, Any],
    prefix: str = "",
) -> tuple[dict[str, tuple[float, float]], list[str]]:
    """递归对比两个结果字典，返回标量变化与排名翻转。"""
    scalars: dict[str, tuple[float, float]] = {}
    flips: list[str] = []

    for key in sorted(set(before) | set(after)):
        path = f"{prefix}{key}"
        old, new = before.get(key), after.get(key)

        if isinstance(old, (int, float)) and isinstance(new, (int, float)) \
                and not isinstance(old, bool) and not isinstance(new, bool):
            if old != new:
                scalars[path] = (float(old), float(new))
        elif isinstance(old, list) and isinstance(new, list):
            if old != new and _is_ranking(old) and _is_ranking(new):
                flips.append(path)
        elif isinstance(old, dict) and isinstance(new, dict):
            sub_scalars, sub_flips = _diff(old, new, prefix=f"{path}.")
            scalars.update(sub_scalars)
            flips.extend(sub_flips)

    return scalars, flips


def _is_ranking(value: list[Any]) -> bool:
    """字符串/数字列表视为排名序列，其顺序变化即结论翻转。"""
    return len(value) > 0 and all(isinstance(item, (str, int, float)) for item in value)


def _relative_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0 if new == 0 else 1.0
    return abs(new - old) / abs(old)


def _summarize(
    overrides: dict[str, Any],
    scalars: dict[str, tuple[float, float]],
    flips: list[str],
    flipped: bool,
) -> str:
    change_text = "、".join(f"{k}={v}" for k, v in overrides.items())
    if flipped:
        parts: list[str] = []
        if flips:
            parts.append(f"排序字段 {', '.join(flips)} 发生变化")
        big = [f"{k}（{o:g} → {n:g}）" for k, (o, n) in scalars.items()
               if _relative_change(o, n) >= FLIP_THRESHOLD]
        if big:
            parts.append(f"显著变化指标：{'、'.join(big[:3])}")
        return f"参数 {change_text} 后结论发生翻转：{'；'.join(parts)}"

    if not scalars:
        return f"参数 {change_text} 后结果完全一致，结论稳健"
    return f"参数 {change_text} 后数值有变化但结论未翻转（{len(scalars)} 项指标变动）"
