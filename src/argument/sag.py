"""SAG（空间论证图）核心数据模型。

五个构件与 Toulmin 论证模型的对应关系::

    Claim      -> Claim（主张）
    Evidence   -> Data / Grounds（论据）
    Operation  -> Warrant（正当理由：可执行算子与参数）
    Provenance -> Backing（支撑：数据源、时间窗、坐标基准）
    Caveat     -> Qualifier + Rebuttal（限定词与反驳条件，工程化合并）

三条结构约束由模型本身强制，不依赖提示词：
1. ``Claim.evidence_ids`` 至少 1 条 —— no-evidence-no-claim 的第一道闸。
2. ``Evidence`` 必须带可执行的 ``Operation`` 与完整 ``Provenance``。
3. ``Claim.metrics`` 中每个数值都要声明来源（raw / derived / external），
   使"证据存在"与"数值为真"成为两件可分别校验的事。

关于 Caveat 的一处说明：Toulmin 中 Qualifier（限定词）与 Rebuttal（反驳
条件）是两个独立要素，本模型将其合并为 ``Caveat.kind`` 的一个枚举维度。
论文表述应写明这是工程化合并，而非理论等价。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

#: 展示用坐标基准。国内合规地图服务（高德 / 腾讯 / 天地图）与库内几何同为此基准。
SRID_DISPLAY = "GCJ-02"

#: 证据类型，限定四类，禁止发散。
EvidenceKind = Literal["grid_agg", "geom_overlay", "point_set", "comparison"]

#: 数值来源。raw=直取字段；derived=公式派生；external=外部实时接口。
MetricSource = Literal["raw", "derived", "external"]

#: 限定条件类型。
CaveatKind = Literal[
    "degradation",    # 算子降级（如等时圈降级为缓冲区）
    "small_sample",   # 小样本
    "approximation",  # 近似算法，存在误差界
    "boundary",       # 参数触及可调区间边界
    "coordinate",     # 坐标基准与展示基准不一致
]


class Provenance(BaseModel):
    """数据血缘：保证证据的时空基准可信。"""

    provenance_id: str
    source_name: str
    time_window: str | None = None
    srid: str = SRID_DISPLAY
    data_version: str = "v1.0"
    snapshot_at: datetime | None = None


class AdjustableParam(BaseModel):
    """支持前端 What-if 敏感性复算的参数元数据。"""

    name: str
    label: str
    current_value: float | int
    min_value: float | int
    max_value: float | int
    step: float | int = 1
    unit: str = ""

    @model_validator(mode="after")
    def _check_range(self) -> AdjustableParam:
        if self.min_value > self.max_value:
            raise ValueError(f"{self.name}: min_value 不能大于 max_value")
        if not self.min_value <= self.current_value <= self.max_value:
            raise ValueError(f"{self.name}: current_value 超出 [min, max] 区间")
        return self

    def at_boundary(self) -> bool:
        """当前值是否已顶到可调区间边界（用于自动生成 boundary 类 Caveat）。"""
        return self.current_value in (self.min_value, self.max_value)


class Operation(BaseModel):
    """可执行算子：保证每条证据都能一键复算。"""

    operation_id: str
    tool_name: str
    template_or_sql: str
    params: dict[str, Any] = Field(default_factory=dict)
    adjustable_params: list[AdjustableParam] = Field(default_factory=list)

    #: 降级链，如 ["amap_isochrone", "postgis_buffer"]。非空且实际发生降级时
    #: 必须生成 degradation 类 Caveat。
    fallback_chain: list[str] = Field(default_factory=list)
    #: 近似算子的误差界（米）。非空即视为近似算法。
    error_bound_m: float | None = None

    def is_executable(self) -> bool:
        """算子是否具备可执行的最小定义。"""
        return bool(self.tool_name.strip()) and bool(self.template_or_sql.strip())

    def used_fallback(self) -> bool:
        """本次执行是否实际走了降级路径。"""
        return bool(self.fallback_chain) and bool(self.params.get("_used_fallback"))


class Caveat(BaseModel):
    """限定条件 / 降级标记，对齐 Toulmin 的 Qualifier + Rebuttal。"""

    kind: CaveatKind
    message: str
    severity: Literal["info", "warning"] = "warning"
    #: 是否由规则自动生成。人工撰写的限定条件应为 False，便于在评测中区分。
    auto_generated: bool = True
    #: 触发该 Caveat 的规则标识，如 "R2_SMALL_SAMPLE"。
    source_rule: str = ""


class Metric(BaseModel):
    """Claim 中的一个可校验数值。

    这是与"把数值拍平成一个集合再找成员"的关键区别：每个数值都必须声明
    它从哪来。派生值给出公式与因子，由系统重算后比对，而不是要求它等于
    某个原始字段。
    """

    name: str
    value: float

    source: MetricSource = "raw"
    #: source=raw 时必填：raw_metrics 中的字段路径，如 "count" 或 "stats.by_cat.dispatch"。
    field: str | None = None
    #: source=derived 时必填：只允许 + - * / % ** 与括号的安全表达式。
    formula: str | None = None
    #: source=derived 时必填：公式中的因子取值，如 {"count": 42, "base_count": 37}。
    factors: dict[str, float] = Field(default_factory=dict)

    #: 容差。缺省时按字段名语义推断。
    abs_tol: float | None = None
    rel_tol: float | None = None

    @model_validator(mode="after")
    def _check_source(self) -> Metric:
        if self.source == "raw" and not self.field:
            raise ValueError(f"{self.name}: source=raw 必须提供 field")
        if self.source == "derived":
            if not self.formula:
                raise ValueError(f"{self.name}: source=derived 必须提供 formula")
            if not self.factors:
                raise ValueError(f"{self.name}: source=derived 必须提供 factors")
        if self.source == "external" and self.field:
            raise ValueError(f"{self.name}: source=external 不引用本地字段")
        return self


class Evidence(BaseModel):
    """空间证据：限定四类，绑定地图图层与结果摘要。"""

    evidence_id: str
    kind: EvidenceKind
    summary: str
    raw_metrics: dict[str, Any] = Field(default_factory=dict)
    layer_id: str | None = None
    #: 结果哈希，用于复算一致性比对。留空时由 SAG 组装阶段填入。
    digest: str = ""
    operation: Operation
    provenance: Provenance

    #: 参与计算的样本量，用于自动判定小样本。None 表示不适用。
    sample_size: int | None = None


class Claim(BaseModel):
    """空间结论：必须绑定至少 1 条合法 Evidence。"""

    claim_id: str
    text: str

    metrics: list[Metric] = Field(default_factory=list)
    extracted_entities: list[str] = Field(default_factory=list)

    #: 结构硬约束：至少绑定 1 条证据。由 pydantic 在构造时拦截空列表。
    evidence_ids: list[str] = Field(min_length=1)

    caveats: list[Caveat] = Field(default_factory=list)
    verified: bool = False

    #: 兼容简化写法 {"count": 42}：按 raw 指标、字段名同名解析。
    #: 仅在 metrics 为空时参与校验，且无法解析时降级为警告而非直接拒绝。
    extracted_numbers: dict[str, float] = Field(default_factory=dict)

    @property
    def all_metrics(self) -> list[Metric]:
        """汇总显式 metrics 与简化写法的 extracted_numbers。"""
        metrics = list(self.metrics)
        declared = {m.name for m in metrics}
        for name, value in self.extracted_numbers.items():
            if name in declared:
                continue
            metrics.append(Metric(name=name, value=value, source="raw", field=name))
        return metrics


class SAG(BaseModel):
    """完整空间论证图。"""

    sag_id: str
    thread_id: str
    user_query: str
    claims: list[Claim] = Field(default_factory=list)
    #: evidence_id -> Evidence
    evidences: dict[str, Evidence] = Field(default_factory=dict)
    #: 被拦截的结论记录（"claim_id: 原因"），用于评测统计与案例复盘。
    rejected_claims: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def evidence_of(self, claim: Claim) -> list[Evidence]:
        """返回 claim 实际绑定且存在的证据。"""
        return [self.evidences[i] for i in claim.evidence_ids if i in self.evidences]
