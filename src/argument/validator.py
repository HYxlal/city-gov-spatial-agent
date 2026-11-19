"""SAG 出口硬校验。

四层递进，对应论文第 4 章"结构可溯源"与"内容可验证"的分离：

L1 结构层：证据存在、算子可执行 —— 拦住"没有证据的结论"。
L2 绑定层：每个数值必须能在证据里找到出处 —— 拦住"挂着合法证据编数字"。
L3 语义层：方向词与数值符号一致 —— 拦住"数字对但说反了"。
L4 限定层：Caveat 双向核对 —— 该声明的降级没声明，或凭空编造限定条件。

与常见实现的两处关键差异：

1. **派生数值可校验**。结论里的百分比、均值、占比不是原始字段值，若按
   "数值在不在证据里"判断会大面积误杀正确结论。这里要求派生值给出公式
   与因子，由系统用 AST 安全重算后比对。
2. **派生因子也必须可追溯**。允许派生而不校验因子，等于给 LLM 开了个更
   大的后门（因子随便填，公式算出任意结果）。因此因子值同样要在绑定证据
   的数值域中有出处。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from src.argument.caveat import caveat_kinds, derive_caveats
from src.argument.expr import FormulaError, approx_equal, default_tolerance, get_path, safe_eval
from src.argument.normalize import collect_scalars, entity_present
from src.argument.sag import Claim, Evidence, Metric, SAG

# ---- 问题代码 ----------------------------------------------------------------

E_NO_EVIDENCE = "E_NO_EVIDENCE"          # 未绑定任何证据
E_EVIDENCE_MISSING = "E_EVIDENCE_MISSING"  # 引用的证据不存在
E_OPERATION_INVALID = "E_OPERATION_INVALID"  # 算子不可执行
E_RAW_FIELD_MISSING = "E_RAW_FIELD_MISSING"  # 字段不存在
E_METRIC_MISMATCH = "E_METRIC_MISMATCH"    # 数值与证据不符（疑似虚假绑定）
E_FORMULA_INVALID = "E_FORMULA_INVALID"    # 派生公式非法
E_FACTOR_UNTRACED = "E_FACTOR_UNTRACED"    # 派生因子在证据中无出处
E_ENTITY_UNTRACED = "E_ENTITY_UNTRACED"    # 实体未在证据中出现
E_DIRECTION_CONFLICT = "E_DIRECTION_CONFLICT"  # 方向词与数值符号相反

W_FUZZY_METRIC = "W_FUZZY_METRIC"          # 简化写法退化为值域模糊匹配
W_EXTERNAL_UNVERIFIABLE = "W_EXTERNAL_UNVERIFIABLE"  # 外部实时值无法本地复算
W_CAVEAT_MISSING = "W_CAVEAT_MISSING"      # 应有 Caveat 未声明
W_CAVEAT_UNSUPPORTED = "W_CAVEAT_UNSUPPORTED"  # 声明了规则不支撑的 Caveat

IssueSeverity = Literal["error", "warning"]

#: 上升 / 下降方向词。仅用于与"变化量类指标"的符号比对。
_UP_WORDS = ("上升", "增长", "增加", "升高", "提高", "加剧", "恶化", "偏高", "高于", "上涨")
_DOWN_WORDS = ("下降", "减少", "降低", "减小", "缩小", "缓解", "改善", "偏低", "低于", "下跌")
#: 变化量类指标的名称特征：只有这类指标的正负号才具有方向语义。
_DELTA_HINTS = ("pct", "delta", "change", "mom", "yoy", "环比", "同比", "变化", "增减", "涨跌")


class ClaimIssue(BaseModel):
    """单条校验问题。code 用于评测统计，message 用于人工排查。"""

    claim_id: str
    code: str
    message: str
    severity: IssueSeverity = "error"


class ValidationReport(BaseModel):
    """校验报告。可直接序列化给前端论证面板与评测脚本。"""

    passed: bool
    level: Literal["strict", "lenient"] = "strict"
    verified_claim_ids: list[str] = Field(default_factory=list)
    issues: list[ClaimIssue] = Field(default_factory=list)
    needs_replan: bool = False

    @property
    def errors(self) -> list[ClaimIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ClaimIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def stats(self) -> dict[str, int]:
        """按问题代码汇总计数，用于评测表格与前后端联调。"""
        counts: dict[str, int] = {"verified": len(self.verified_claim_ids)}
        for issue in self.issues:
            counts[issue.code] = counts.get(issue.code, 0) + 1
        return counts


def validate_sag(
    sag: SAG,
    *,
    level: Literal["strict", "lenient"] = "strict",
    check_direction: bool = True,
    check_caveats: bool = True,
) -> ValidationReport:
    """对整张论证图做出口校验。

    Args:
        level: strict 下绑定失败即拒绝；lenient 下 L2 失败降为警告（用于
            调试与消融实验中的宽松对照）。
        check_direction: 是否启用 L3 方向一致性校验。
        check_caveats: 是否启用 L4 Caveat 双向核对。
    """
    issues: list[ClaimIssue] = []
    verified: list[str] = []

    if not sag.claims:
        return ValidationReport(
            passed=False,
            level=level,
            issues=[ClaimIssue(claim_id="-", code=E_NO_EVIDENCE, message="论证图不含任何结论")],
            needs_replan=True,
        )

    for claim in sag.claims:
        claim_issues = _validate_claim(
            sag,
            claim,
            level=level,
            check_direction=check_direction,
            check_caveats=check_caveats,
        )
        issues.extend(claim_issues)
        blocked = any(i.severity == "error" for i in claim_issues)
        claim.verified = not blocked
        if blocked:
            reason = "; ".join(i.message for i in claim_issues if i.severity == "error")
            sag.rejected_claims.append(f"{claim.claim_id}: {reason}")
        else:
            verified.append(claim.claim_id)

    has_error = any(i.severity == "error" for i in issues)
    return ValidationReport(
        passed=not has_error and bool(verified),
        level=level,
        verified_claim_ids=verified,
        issues=issues,
        needs_replan=has_error,
    )


def _validate_claim(
    sag: SAG,
    claim: Claim,
    *,
    level: Literal["strict", "lenient"],
    check_direction: bool,
    check_caveats: bool,
) -> list[ClaimIssue]:
    issues: list[ClaimIssue] = []
    binding_failures: list[ClaimIssue] = []

    def add(code: str, message: str, *, binding: bool = True) -> None:
        """绑定类问题在 lenient 模式下降级为警告。"""
        if binding and level == "lenient":
            issues.append(ClaimIssue(claim_id=claim.claim_id, code=code,
                                     message=message, severity="warning"))
        else:
            issues.append(ClaimIssue(claim_id=claim.claim_id, code=code, message=message))

    # ---- L1 结构层 ------------------------------------------------------
    # 结构问题在 lenient 模式下同样视为 error：no-evidence-no-claim 是硬约束，
    # 不存在"宽松地允许没有证据"这种情况。
    if not claim.evidence_ids:
        add(E_NO_EVIDENCE, "违反 no-evidence-no-claim：未绑定任何 Evidence", binding=False)
        return issues

    evidences: list[Evidence] = []
    for evidence_id in claim.evidence_ids:
        evidence = sag.evidences.get(evidence_id)
        if evidence is None:
            add(E_EVIDENCE_MISSING, f"引用的 Evidence 不存在: {evidence_id}", binding=False)
            continue
        if not evidence.operation.is_executable():
            add(E_OPERATION_INVALID,
                f"Evidence({evidence_id}) 的 Operation 缺少可执行定义"
                "（tool_name 或 template_or_sql 为空）", binding=False)
            continue
        evidences.append(evidence)

    if not evidences:
        return issues

    # ---- L2 绑定层：数值与实体 ------------------------------------------
    fuzzy_names = _fuzzy_metric_names(claim)
    factor_pool = _numeric_pool(evidences)

    for metric in claim.all_metrics:
        if metric.source == "external":
            issues.append(ClaimIssue(
                claim_id=claim.claim_id,
                code=W_EXTERNAL_UNVERIFIABLE,
                message=f"指标 {metric.name} 来自外部实时接口，无法本地复算，已记录数据源",
                severity="warning",
            ))
            continue

        ok, detail = _resolve_metric(metric, evidences, allow_fuzzy=metric.name in fuzzy_names)
        if ok:
            if detail.startswith("FUZZY:"):
                issues.append(ClaimIssue(
                    claim_id=claim.claim_id,
                    code=W_FUZZY_METRIC,
                    message=f"指标 {metric.name} 由简化写法解析，仅做值域匹配：{detail[6:]}",
                    severity="warning",
                ))
            continue
        add(E_METRIC_MISMATCH, f"数值矛盾（疑似虚假绑定）: {metric.name}={metric.value:g}；{detail}")

    for entity in claim.extracted_entities:
        if not entity_present(entity, _entity_pool(evidences)):
            add(E_ENTITY_UNTRACED, f"实体未在绑定证据中出现: {entity!r}")

    # ---- L3 语义层：方向一致性 ------------------------------------------
    if check_direction:
        direction_issue = _check_direction(claim)
        if direction_issue:
            add(E_DIRECTION_CONFLICT, direction_issue)

    # ---- L4 限定层：Caveat 双向核对 -------------------------------------
    if check_caveats:
        required = _required_caveats(evidences)
        required_kinds = caveat_kinds(required)
        actual_kinds = caveat_kinds(claim.caveats)
        for kind in sorted(required_kinds - actual_kinds):
            sample = next(c for c in required if c.kind == kind)
            issues.append(ClaimIssue(
                claim_id=claim.claim_id,
                code=W_CAVEAT_MISSING,
                message=f"缺少应由规则 {sample.source_rule} 自动生成的 {kind} 限定条件：{sample.message}",
                severity="warning",
            ))
        for kind in sorted(actual_kinds - required_kinds):
            issues.append(ClaimIssue(
                claim_id=claim.claim_id,
                code=W_CAVEAT_UNSUPPORTED,
                message=f"声明了规则不支撑的 {kind} 限定条件（疑似编造）",
                severity="warning",
            ))

    return issues


# ---- 内部实现 ----------------------------------------------------------------


def _fuzzy_metric_names(claim: Claim) -> set[str]:
    """来自 extracted_numbers 简化写法、且未在 metrics 中显式声明的指标名。"""
    declared = {m.name for m in claim.metrics}
    return {name for name in claim.extracted_numbers if name not in declared}


def _resolve_metric(
    metric: Metric,
    evidences: list[Evidence],
    *,
    allow_fuzzy: bool,
) -> tuple[bool, str]:
    """解析单个指标的出处。返回 (是否通过, 说明)。"""
    if metric.source == "derived":
        return _resolve_derived(metric, evidences)
    return _resolve_raw(metric, evidences, allow_fuzzy=allow_fuzzy)


def _resolve_raw(
    metric: Metric,
    evidences: list[Evidence],
    *,
    allow_fuzzy: bool,
) -> tuple[bool, str]:
    field = metric.field or metric.name
    for evidence in evidences:
        try:
            actual = get_path(evidence.raw_metrics, field)
        except KeyError:
            continue
        actual_value = _as_float(actual)
        if actual_value is None:
            continue
        abs_tol, rel_tol = _tolerance(metric, actual_value)
        if approx_equal(metric.value, actual_value, abs_tol, rel_tol):
            return True, f"命中 evidence={evidence.evidence_id} field={field} actual={actual_value:g}"
        return False, (
            f"字段 {field} 在 evidence={evidence.evidence_id} 中实际值为 {actual_value:g}，"
            f"与声明值不符（容差 abs={abs_tol:g} rel={rel_tol:g}）"
        )

    if allow_fuzzy:
        # 简化写法兜底：按数值在证据值域中查找。通过但记警告，因为丢失了字段归属。
        _, numbers = collect_scalars([e.raw_metrics for e in evidences])
        for candidate in numbers:
            abs_tol, rel_tol = _tolerance(metric, candidate)
            if approx_equal(metric.value, candidate, abs_tol, rel_tol):
                return True, f"FUZZY:值域中存在 {candidate:g}，但无法确定字段归属"
        return False, f"值域中未找到 {metric.value:g}"

    return False, f"绑定证据中不存在字段 {field}"


def _resolve_derived(metric: Metric, evidences: list[Evidence]) -> tuple[bool, str]:
    try:
        expected = safe_eval(metric.formula or "", metric.factors)
    except FormulaError as exc:
        return False, f"派生公式非法：{exc}"

    # 因子可追溯性：允许派生但不校验因子，等于允许编造任意结果。
    pool = _numeric_pool(evidences)
    for factor_name, factor_value in metric.factors.items():
        if not any(_close_by_scale(factor_value, candidate) for candidate in pool):
            return False, (
                f"派生因子 {factor_name}={factor_value:g} 在绑定证据的数值中无出处，"
                "无法确认该因子来自数据而非模型推测"
            )

    abs_tol, rel_tol = _tolerance(metric, expected)
    if approx_equal(metric.value, expected, abs_tol, rel_tol):
        return True, f"派生复算一致：{metric.formula} = {expected:.4f}"
    return False, (
        f"派生复算不一致：按公式 {metric.formula} 与因子重算得 {expected:.4f}，"
        f"与声明值 {metric.value:g} 相差 {abs(metric.value - expected):.4f}"
    )


def _check_direction(claim: Claim) -> str | None:
    """方向词与变化量指标符号是否一致。

    只对"变化量类指标"校验符号，避免"投诉量下降到 42 起"这类表述被误判。
    """
    metrics = [m for m in claim.all_metrics if _is_delta_metric(m.name)]
    if not metrics:
        return None

    has_up = any(word in claim.text for word in _UP_WORDS)
    has_down = any(word in claim.text for word in _DOWN_WORDS)
    if has_up == has_down:  # 都没提，或同时出现（混合表述），不做判断
        return None

    values = [m.value for m in metrics]
    if has_up and not any(v > 0 for v in values):
        return (f"结论称上升/增长，但变化量指标符号均为非正："
                f"{ {m.name: m.value for m in metrics} }")
    if has_down and not any(v < 0 for v in values):
        return (f"结论称下降/减少，但变化量指标符号均为非负："
                f"{ {m.name: m.value for m in metrics} }")
    return None


def _required_caveats(evidences: list[Evidence]) -> list:
    from src.argument.sag import Caveat  # 局部导入避免循环

    required: list[Caveat] = []
    for evidence in evidences:
        for caveat in derive_caveats([evidence], evidence.operation, evidence.provenance):
            if caveat.kind not in caveat_kinds(required):
                required.append(caveat)
    return required


def _numeric_pool(evidences: list[Evidence]) -> list[float]:
    pool: list[float] = []
    for evidence in evidences:
        _, numbers = collect_scalars(evidence.raw_metrics)
        pool.extend(numbers)
    return pool


def _entity_pool(evidences: list[Evidence]) -> set[str]:
    entities: set[str] = set()
    for evidence in evidences:
        strings, _ = collect_scalars(evidence.raw_metrics)
        entities.update(strings)
    return entities


def _is_delta_metric(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _DELTA_HINTS)


def _tolerance(metric: Metric, actual: float) -> tuple[float, float]:
    abs_tol, rel_tol = default_tolerance(metric.name, actual)
    if metric.abs_tol is not None:
        abs_tol = metric.abs_tol
    if metric.rel_tol is not None:
        rel_tol = metric.rel_tol
    return abs_tol, rel_tol


def _close_by_scale(a: float, b: float) -> bool:
    """因子的宽松一致性判断（因子可能与证据中同值不同单位的量并存）。"""
    return approx_equal(a, b, abs_tol=0.5, rel_tol=0.02)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
