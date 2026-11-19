"""DDL 的语法校验与结构一致性检查。

用 pglast（PostgreSQL 官方解析器 libpg_query 的绑定）真实解析 SQL，
而不是靠正则或肉眼——语法错误在导入数据库时才会暴露，那时已经浪费半天。

第二类检查更重要：**库表字段与 Pydantic 模型字段是否对齐**。
模型改了而 DDL 没改（或反之），是这类项目最常见、也最难排查的漂移。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pglast import parse_sql
from pglast.ast import ColumnDef, CreateStmt

from src.argument.sag import Claim, Evidence, Operation, Provenance

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
SCHEMA_SQL = (SQL_DIR / "01_schema.sql").read_text(encoding="utf-8")
SAG_SQL = (SQL_DIR / "02_sag_tables.sql").read_text(encoding="utf-8")


def _tables(sql: str) -> dict[str, list[str]]:
    """提取 CREATE TABLE 的表名与列名。"""
    tables: dict[str, list[str]] = {}
    for raw in parse_sql(sql):
        stmt = raw.stmt
        if isinstance(stmt, CreateStmt) and not stmt.partbound:
            name = stmt.relation.relname
            tables[name] = [c.colname for c in stmt.tableElts if isinstance(c, ColumnDef)]
    return tables


class TestParse:
    def test_business_schema_parses(self) -> None:
        statements = parse_sql(SCHEMA_SQL)
        assert len(statements) >= 20

    def test_sag_schema_parses(self) -> None:
        statements = parse_sql(SAG_SQL)
        assert len(statements) >= 10

    def test_no_syntax_error_in_either_file(self) -> None:
        """两个文件都不应抛 ParseError。"""
        for sql in (SCHEMA_SQL, SAG_SQL):
            parse_sql(sql)


class TestBusinessTables:
    def test_expected_tables_exist(self) -> None:
        tables = _tables(SCHEMA_SQL)
        for name in (
            "dim_district", "dim_grid", "dim_category", "dim_poi",
            "dim_resource", "fact_event", "metric_dict",
        ):
            assert name in tables, f"缺少表 {name}"

    def test_fact_event_partitioned_by_month(self) -> None:
        assert "PARTITION BY RANGE (reported_at)" in SCHEMA_SQL
        partitions = SCHEMA_SQL.count("PARTITION OF fact_event")
        assert partitions == 12, f"应覆盖 12 个自然月，实际 {partitions} 个分区"

    def test_grid_declares_coordinate_basis(self) -> None:
        """坐标基准必须显式声明，不能靠约定。"""
        assert "srid_source" in SCHEMA_SQL
        assert "'GCJ-02'" in SCHEMA_SQL

    def test_grid_has_separate_compute_geometry(self) -> None:
        """展示列与计算列分离：3857 专用于面积/距离。"""
        assert "geom_3857" in SCHEMA_SQL
        assert "geometry(Polygon, 3857)" in SCHEMA_SQL


class TestSagTables:
    def test_five_tables_exist(self) -> None:
        tables = _tables(SAG_SQL)
        for name in (
            "sag_graph", "sag_provenance", "sag_operation",
            "sag_evidence", "sag_claim",
        ):
            assert name in tables, f"缺少表 {name}"

    def test_no_evidence_no_claim_at_db_level(self) -> None:
        """数据库层也要兜住：绕过 Python 直接写库同样不允许空证据。"""
        assert "cardinality(evidence_ids) >= 1" in SAG_SQL

    def test_evidence_kind_enumerated(self) -> None:
        for kind in ("grid_agg", "geom_overlay", "point_set", "comparison"):
            assert kind in SAG_SQL

    def test_digest_format_constrained(self) -> None:
        assert "digest LIKE 'sha256:%'" in SAG_SQL

    def test_quality_views_defined(self) -> None:
        for view in ("v_sag_caveat", "v_sag_quality", "v_sag_metric_summary"):
            assert f"CREATE VIEW {view}" in SAG_SQL


class TestModelSchemaAlignment:
    """模型与库表字段一致性。字段改名时，这里会先失败。"""

    #: 模型字段名 -> 库表列名（语义相同但命名不同）
    FIELD_ALIASES = {
        Claim: {"text": "claim_text"},
        # 模型里是嵌套对象，表里落为外键引用
        Evidence: {"operation": "operation_id", "provenance": "provenance_id"},
        Operation: {},
        Provenance: {},
    }
    TABLE_OF = {
        Claim: "sag_claim",
        Evidence: "sag_evidence",
        Operation: "sag_operation",
        Provenance: "sag_provenance",
    }
    #: 仅运行时使用的字段，不落库。落库的是归一化后的结果——
    #: extracted_numbers 是"简化写法"的输入便捷字段，校验时已展开进 metrics，
    #: 存 metrics 即可复现；再存一份原文只会带来两份真值不一致的风险。
    RUNTIME_ONLY = {
        Claim: {"extracted_numbers"},
        Evidence: set(),
        Operation: set(),
        Provenance: set(),
    }

    @pytest.mark.parametrize("model", [Claim, Evidence, Operation, Provenance])
    def test_every_model_field_has_column(self, model) -> None:
        columns = _tables(SAG_SQL)[self.TABLE_OF[model]]
        aliases = self.FIELD_ALIASES[model]
        runtime_only = self.RUNTIME_ONLY[model]
        missing = [
            field for field in model.model_fields
            if field not in runtime_only and aliases.get(field, field) not in columns
        ]
        assert not missing, (
            f"{model.__name__} 的字段 {missing} 在 {self.TABLE_OF[model]} 中无对应列"
        )

    def test_no_column_orphaned_from_model(self) -> None:
        """反向检查：表里有列但模型里没有对应字段，说明 DDL 没跟上模型改动。"""
        ignore = {"sag_id", "thread_id", "created_at", "reject_reason"}
        aliases = self.FIELD_ALIASES[Claim]
        columns = set(_tables(SAG_SQL)["sag_claim"])
        model_columns = {
            aliases.get(f, f) for f in Claim.model_fields
            if f not in self.RUNTIME_ONLY[Claim]
        }
        orphaned = columns - model_columns - ignore
        assert not orphaned, f"sag_claim 中多出无模型对应列的字段: {sorted(orphaned)}"
