"""S1 端到端演示：从合成数据 → 算子 → 证据 → 结论 → 校验 → What-if。

为什么不接数据库也能演示：本文件用纯 Python 复现了 SQL 模板的语义
（``GROUP BY h3_idx ORDER BY count DESC LIMIT topk``），数据从 CSV 读入。
这样在没有 PostgreSQL 的机器上也能跑通完整论证链——对答辩现场尤其重要，
不能把演示押在"实验室那台装了 PostGIS 的机器"上。

跑通后你会看到四件事：
1. 一条正常结论如何挂上证据并通过校验；
2. 数值正确但把"上升"说成"下降"会被 L3 拦下；
3. 编造数值会被 L2 拦下；
4. 把时间窗从 30 天改成 90 天，结论是否翻转（What-if）。

运行::

    python examples/s1_end_to_end.py
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3

from src.argument.caveat import derive_caveats
from src.argument.digest import digest_of_evidence
from src.argument.replay import replay_operation
from src.argument.sag import (
    AdjustableParam,
    Claim,
    Evidence,
    Metric,
    Operation,
    Provenance,
    SAG,
)
from src.argument.validator import validate_sag

DATA_DIR = ROOT / "data" / "generated"
CST = timezone(timedelta(hours=8))
#: 分析基准时刻：取数据窗口内的一天，避免用到"未来"数据
REF_TIME = datetime(2026, 11, 18, tzinfo=CST)
CATEGORY = "ZDJY"
CATEGORY_NAME = "占道经营"
RADIUS_M = 1000


# ---- 数据加载 ----------------------------------------------------------------


def ensure_data() -> dict[str, Any]:
    """数据不存在时自动生成，保证脚本开箱即跑。"""
    if not (DATA_DIR / "manifest.json").exists():
        spec = importlib.util.spec_from_file_location(
            "gen", ROOT / "data" / "generate_synthetic_data.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.generate(DATA_DIR)

    return json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))


def load_events() -> list[dict[str, Any]]:
    with (DATA_DIR / "fact_event.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["reported_dt"] = datetime.fromisoformat(row["reported_at"])
    return rows


# ---- 算子（复现 SQL 模板语义）-------------------------------------------------


def query_events_by_grid(
    events: list[dict[str, Any]],
    *,
    category: str,
    days: int,
    topk: int,
    ref: datetime = REF_TIME,
) -> dict[str, Any]:
    """等价于：

    SELECT h3_idx, count(*) FROM fact_event
    WHERE category_code = :category AND reported_at >= :ref - :days
    GROUP BY h3_idx ORDER BY count DESC LIMIT :topk
    """
    start = ref - timedelta(days=days)
    counter: Counter[str] = Counter(
        event["h3_idx"]
        for event in events
        if event["category_code"] == category and start <= event["reported_dt"] < ref
    )
    top = counter.most_common(topk)
    return {
        "h3_idx": top[0][0] if top else None,
        "count": sum(count for _grid, count in top),
        "top_ranking": [grid for grid, _count in top],
        "by_grid": {grid: count for grid, count in top},
        "total_events": sum(counter.values()),
    }


def query_nearby_resources(
    resources: list[dict[str, Any]],
    center_cell: str,
    radius_m: int,
) -> dict[str, Any]:
    """等价于 ST_DWithin(geom, center, radius) 的近似实现。

    用 H3 环形邻域近似圆，因此结果带误差——这个误差不是隐藏的，
    会通过 Operation.error_bound_m 变成一条 approximation 类型的 Caveat。
    """
    ring = max(1, round(radius_m / 800))  # res 8 格边长约 0.8 km
    cells = set(h3.grid_disk(center_cell, ring))
    nearby = [r for r in resources if r["h3_idx"] in cells]
    return {
        "resource_count": len(nearby),
        "resource_ranking": [r["name"] for r in nearby],
        "resource_types": sorted({r["resource_type"] for r in nearby}),
        "approx_cells": len(cells),
    }


# ---- 论证图组装 --------------------------------------------------------------


def build_operation(days: int = 30, topk: int = 5) -> Operation:
    return Operation(
        operation_id="op_top_grids_events",
        tool_name="query_events_by_grid",
        template_or_sql=(
            "SELECT h3_idx, count(*) AS count FROM fact_event "
            "WHERE category_code = %(category)s "
            "AND reported_at >= %(ref)s::timestamptz - interval '%(days)s days' "
            "GROUP BY h3_idx ORDER BY count DESC LIMIT %(topk)s"
        ),
        params={"category": CATEGORY, "days": days, "topk": topk},
        adjustable_params=[
            AdjustableParam(name="days", label="统计天数", current_value=days,
                            min_value=7, max_value=180, step=7, unit="天"),
            AdjustableParam(name="topk", label="返回网格数", current_value=topk,
                            min_value=3, max_value=20, step=1, unit="个"),
        ],
    )


def build_provenance(data_version: str) -> Provenance:
    return Provenance(
        provenance_id="prov_fact_event",
        source_name="fact_event",
        time_window=f"{REF_TIME.date()} 前 30 天",
        srid="GCJ-02",
        data_version=data_version,
    )


def build_evidence(
    events: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    operation: Operation,
    provenance: Provenance,
    data_version: str,
) -> list[Evidence]:
    days = int(operation.params["days"])
    topk = int(operation.params["topk"])

    current = query_events_by_grid(events, category=CATEGORY, days=days, topk=topk)
    previous = query_events_by_grid(
        events, category=CATEGORY, days=days, topk=topk,
        ref=REF_TIME - timedelta(days=days),
    )

    top1 = current["h3_idx"]
    nearby = query_nearby_resources(resources, top1, RADIUS_M)

    grid_metrics = {
        "h3_idx": top1,
        "count": current["count"],
        "base_count": previous["count"],
        "top_ranking": current["top_ranking"],
        "by_grid": current["by_grid"],
        "total_events": current["total_events"],
    }

    resource_op = Operation(
        operation_id="op_nearby_resources",
        tool_name="query_nearby_resources",
        template_or_sql="SELECT * FROM dim_resource WHERE ST_DWithin(geom, :center, :radius)",
        params={"center_h3": top1, "radius_m": RADIUS_M},
        adjustable_params=[
            AdjustableParam(name="radius_m", label="搜索半径", current_value=RADIUS_M,
                            min_value=300, max_value=3000, step=100, unit="米"),
        ],
        error_bound_m=radius_m_error(RADIUS_M),
    )

    return [
        Evidence(
            evidence_id="ev_top_grids",
            kind="grid_agg",
            summary=(
                f"近 {days} 天{CATEGORY_NAME}共 {current['total_events']} 起，"
                f"Top{topk} 网格合计 {current['count']} 起，首位为 {top1}"
            ),
            raw_metrics=grid_metrics,
            layer_id="layer_top_grids",
            digest=digest_of_evidence(grid_metrics, operation.params),
            operation=operation,
            provenance=provenance,
            sample_size=current["total_events"],
        ),
        Evidence(
            evidence_id="ev_nearby_resources",
            kind="point_set",
            summary=f"首位网格周边约 {RADIUS_M} 米内有 {nearby['resource_count']} 个处置力量点",
            raw_metrics=nearby,
            layer_id="layer_resources",
            digest=digest_of_evidence(nearby, resource_op.params),
            operation=resource_op,
            provenance=provenance,
        ),
    ]


def radius_m_error(radius_m: int) -> float:
    """环形近似带来的最大误差：外接环比目标半径多出的距离。"""
    ring = max(1, round(radius_m / 800))
    return float(abs(ring * 800 - radius_m))


# ---- 主流程 ------------------------------------------------------------------


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pytest 捕获对象可能不支持
            pass

    manifest = ensure_data()
    data_version = manifest["data_version"]
    events = load_events()

    with (DATA_DIR / "dim_resource.csv").open(encoding="utf-8", newline="") as handle:
        resources = list(csv.DictReader(handle))

    operation = build_operation(days=30, topk=5)
    provenance = build_provenance(data_version)
    evidences = build_evidence(events, resources, operation, provenance, data_version)

    grid_ev = evidences[0]
    resource_ev = evidences[1]
    current_count = grid_ev.raw_metrics["count"]
    base_count = grid_ev.raw_metrics["base_count"]
    change = (current_count - base_count) / base_count * 100
    top1 = grid_ev.raw_metrics["h3_idx"]
    resource_count = resource_ev.raw_metrics["resource_count"]
    resource_names = resource_ev.raw_metrics["resource_ranking"]

    # 方向词由数据决定；下面"说反话"的那条用例则刻意取相反词
    direction = "上升" if change >= 0 else "下降"
    opposite = "下降" if change >= 0 else "上升"

    def caveats_for(*evidence_list: Evidence) -> list:
        """每条证据用**它自己的算子**推导 Caveat，再按类型去重。

        传外层 operation 会漏掉资源算子的近似误差——那正是最该被声明的限定。
        """
        merged: list = []
        for evidence in evidence_list:
            for caveat in derive_caveats([evidence], evidence.operation, evidence.provenance):
                if caveat.kind not in {item.kind for item in merged}:
                    merged.append(caveat)
        return merged

    grid_caveats = caveats_for(grid_ev)
    full_caveats = caveats_for(grid_ev, resource_ev)

    print(f"数据版本 {data_version}  事件 {len(events)} 条")
    print(f"分析：近 30 天{CATEGORY_NAME}，Top5 网格合计 {current_count} 起，"
          f"上一周期 {base_count} 起，环比 {change:+.1f}%")
    print(f"首位网格 {top1}，周边 {RADIUS_M} 米内处置力量 {resource_count} 个")
    print(f"自动生成 Caveat {len(full_caveats)} 条："
          f"{[c.kind for c in full_caveats] or '（无）'}")
    print()

    claims = [
        Claim(
            claim_id="c_good",
            text=(f"近 30 天{CATEGORY_NAME}首位网格为 {top1}，"
                  f"Top5 合计 {current_count} 起，环比{direction} {abs(change):.1f}%；"
                  f"周边 {RADIUS_M} 米内仅有 {resource_count} 个处置力量点。"),
            metrics=[
                Metric(name="count", value=current_count, source="raw", field="count"),
                Metric(
                    name="mom_pct", value=round(change, 1), source="derived",
                    formula="(count - base_count) / base_count * 100",
                    factors={"count": current_count, "base_count": base_count},
                ),
                Metric(name="resource_count", value=resource_count,
                       source="raw", field="resource_count"),
            ],
            extracted_entities=[top1] + ([resource_names[0]] if resource_names else []),
            evidence_ids=["ev_top_grids", "ev_nearby_resources"],
            caveats=full_caveats,
        ),
        Claim(
            claim_id="c_wrong_direction",
            text=f"近 30 天{CATEGORY_NAME}首位网格为 {top1}，Top5 合计环比{opposite}。",
            metrics=[
                Metric(
                    name="mom_pct", value=round(change, 1), source="derived",
                    formula="(count - base_count) / base_count * 100",
                    factors={"count": current_count, "base_count": base_count},
                ),
            ],
            evidence_ids=["ev_top_grids"],
            caveats=grid_caveats,
        ),
        Claim(
            claim_id="c_fabricated",
            text="近 30 天占道经营首位网格工单 3 起。",
            metrics=[Metric(name="count", value=3, source="raw", field="count")],
            evidence_ids=["ev_top_grids"],
            caveats=grid_caveats,
        ),
    ]

    sag = SAG(
        sag_id="sag_s1_demo",
        thread_id="thread_demo",
        user_query="近 30 天占道经营最集中的网格是哪里，环比变化如何？",
        claims=claims,
        evidences={e.evidence_id: e for e in evidences},
    )

    report = validate_sag(sag, check_caveats=True)
    print("=== 出口校验 ===")
    for claim in claims:
        mark = "通过" if claim.verified else "拒绝"
        print(f"  {claim.claim_id:<18} {mark}")
    for issue in report.issues:
        print(f"    [{issue.severity:<7}] {issue.claim_id:<18} {issue.code}: {issue.message[:90]}")
    print(f"  整体 passed={report.passed}  needs_replan={report.needs_replan}")
    print()

    # ---- What-if：把时间窗从 30 天改到 90 天 ----
    def executor(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "query_events_by_grid":
            result = query_events_by_grid(
                events,
                category=params["category"],
                days=int(params["days"]),
                topk=int(params["topk"]),
            )
            return {
                "count": result["count"],
                "top_ranking": result["top_ranking"],
                "by_grid": result["by_grid"],
            }
        raise ValueError(f"未知工具 {tool_name}")

    baseline = executor("query_events_by_grid", dict(operation.params))
    outcome = replay_operation(operation, {"days": 90}, executor, baseline=baseline)

    print("=== What-if 复算（时间窗 30 天 -> 90 天）===")
    print(f"  digest 变化: {outcome.old_digest[:20]}... -> {outcome.new_digest[:20]}...")
    print(f"  结论翻转: {outcome.conclusion_flipped}")
    print(f"  {outcome.summary}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
