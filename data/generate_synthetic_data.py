"""S1 场景合成数据生成器。

设计目标不是"造得像真数据"，而是三条可复现性要求：

1. **固定种子**：同一 seed 两次生成，文件 sha256 必须完全一致；
2. **不依赖数据库**：输出 CSV，任何机器上都能生成、能评测；
3. **数据版本可追溯**：manifest 汇总各文件摘要，写入 ``Provenance.data_version``，
   使"这条结论是基于哪一版数据算出来的"可被回答。

坐标基准：全链路统一使用 GCJ-02 经纬度做 H3 索引。H3 只是球面剖分算法，
用哪个基准的经纬度都能算，但一旦混用（比如 GPS 原始点用 WGS-84 直接入格），
同一位置会落到不同网格，空间聚合结论随即失真。因此生成、入格、展示三步
使用同一基准，并在 ``dim_grid.srid_source`` 里显式声明。

用法::

    python data/generate_synthetic_data.py --out data/generated
    python data/generate_synthetic_data.py --events 2000 --ring 3   # 快速验证
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import h3

#: 数据时间窗：12 个自然月
WINDOW_START = datetime(2025, 12, 1, tzinfo=timezone(timedelta(hours=8)))
WINDOW_END = datetime(2026, 12, 1, tzinfo=timezone(timedelta(hours=8)))

CST = timezone(timedelta(hours=8))

#: 行政区（虚构示范片区，避免使用真实区划数据）
DISTRICT_CODE = "110105"
DISTRICT_NAME = "朝阳示范片区"

#: 事件分类及其权重、处置时限、严重度
CATEGORIES = [
    ("ZDJY", "占道经营", 0.28, 24, "medium"),
    ("LJDF", "垃圾堆放", 0.18, 12, "medium"),
    ("ZSRM", "噪声扰民", 0.16, 48, "low"),
    ("WZTC", "违章停车", 0.20, 6, "high"),
    ("LMPD", "路面破损", 0.10, 72, "medium"),
    ("GXDC", "共享单车堆积", 0.08, 24, "low"),
]

#: 季节性系数：月份 -> 分类 -> 倍数（占道经营、噪声在夏秋偏高）
SEASONAL = {
    6: {"ZDJY": 1.4, "ZSRM": 1.3, "GXDC": 1.2},
    7: {"ZDJY": 1.5, "ZSRM": 1.4, "GXDC": 1.3},
    8: {"ZDJY": 1.45, "ZSRM": 1.35, "GXDC": 1.25},
    9: {"ZDJY": 1.2, "ZSRM": 1.1},
    12: {"ZDJY": 0.7, "LJDF": 1.15},
    1: {"ZDJY": 0.7, "LJDF": 1.2},
    2: {"ZDJY": 0.75, "LJDF": 1.15},
}

#: 每日时段权重：早市、晚高峰两个波峰
HOUR_WEIGHTS = [
    0.2, 0.1, 0.1, 0.1, 0.2, 0.4, 0.8, 1.4, 1.8, 1.6, 1.3, 1.2,
    1.3, 1.2, 1.1, 1.2, 1.5, 1.9, 2.0, 1.7, 1.2, 0.9, 0.6, 0.3,
]

POI_TYPES = [
    ("school", "学校", 800),
    ("hospital", "社区卫生服务站", 300),
    ("market", "菜市场", 500),
    ("toilet", "公共卫生间", 60),
    ("park", "口袋公园", 0),
    ("service", "社区服务中心", 200),
]

RESOURCE_TYPES = [("zhifa", "城管执法队"), ("huanwei", "环卫作业站"), ("xuncha", "综合巡查组")]


# ---- 几何工具 ----------------------------------------------------------------


def _to_mercator(lat: float, lng: float) -> tuple[float, float]:
    """WGS84/GCJ-02 经纬度 -> Web Mercator（EPSG:3857）平面坐标。

    只用于面积与距离计算。近似到米级足够——我们不在这个坐标系里做精确测量，
    精确面积由 PostGIS 的 geography 类型负责。
    """
    x = lng * 20037508.342789244 / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * 20037508.342789244 / math.pi
    return x, y


def _ring_wkt(coords: list[tuple[float, float]]) -> str:
    """把 [(lng, lat), ...] 序列化为闭合环。"""
    points = list(coords)
    if points[0] != points[-1]:
        points.append(points[0])
    return "(" + ", ".join(f"{x:.6f} {y:.6f}" for x, y in points) + ")"


def _cell_polygons(cell: str) -> tuple[str, str]:
    """H3 单元格 -> (4326 WKT, 3857 WKT)。

    两个坐标系的 WKT 必须分别生成。把经纬度数值直接写进 3857 列，导入后会被
    当成"116 米外的点"，所有面积与距离计算随即失效——这正是坐标基准契约要
    防的错误，不能出现在自己的数据管道里。

    h3 v4 的 cell_to_boundary 返回 (lat, lng) 顺序，WKT 需要 (lon lat)。
    """
    boundary = h3.cell_to_boundary(cell)
    ll = [(lng, lat) for lat, lng in boundary]
    mercator = [_to_mercator(lat, lng) for lng, lat in ll]
    return (
        f"POLYGON({_ring_wkt(ll)})",
        f"POLYGON({_ring_wkt([(x, y) for x, y in mercator])})",
    )


def _point_wkt(lat: float, lng: float) -> str:
    return f"POINT({lng:.6f} {lat:.6f})"


def _jitter_in_cell(rng: random.Random, cell: str) -> tuple[float, float]:
    """在网格内取一个随机点：以格心为基准做小范围抖动。"""
    lat, lng = h3.cell_to_latlng(cell)
    edge_km = math.sqrt(h3.cell_area(cell, unit="km^2")) / 2
    d_lat = edge_km / 111.0
    d_lng = edge_km / (111.0 * max(math.cos(math.radians(lat)), 0.2))
    return lat + rng.uniform(-d_lat, d_lat), lng + rng.uniform(-d_lng, d_lng)


def _district_wkt(cells: list[str]) -> str:
    """把网格集合合并为行政区几何。

    优先用 h3 的多边形合并（结果贴合网格边界）；失败时退回外包矩形，
    并在返回值中保持几何合法——行政区几何只用于展示与面积粗算，不参与论证。
    """
    try:
        shape = h3.cells_to_h3shape(cells)
        outer = [(point.lng, point.lat) for point in shape.outer]
        if len(outer) >= 4:
            return f"MULTIPOLYGON(({_ring_wkt(outer)}))"
    except Exception:  # pragma: no cover - 依赖 h3 版本能力
        pass

    lats = [h3.cell_to_latlng(c)[0] for c in cells]
    lngs = [h3.cell_to_latlng(c)[1] for c in cells]
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    box = [
        (min_lng, min_lat), (max_lng, min_lat),
        (max_lng, max_lat), (min_lng, max_lat),
    ]
    return f"MULTIPOLYGON(({_ring_wkt(box)}))"


# ---- 生成流程 ----------------------------------------------------------------


def build_grid(
    rng: random.Random,
    center_lat: float,
    center_lng: float,
    res: int,
    ring: int,
) -> list[dict[str, Any]]:
    """生成 H3 网格维表。热点网格的常住人口与后续事件权重都更高。"""
    center_cell = h3.latlng_to_cell(center_lat, center_lng, res)
    cells = sorted(h3.grid_disk(center_cell, ring))

    grids: list[dict[str, Any]] = []
    for cell in cells:
        area = h3.cell_area(cell, unit="km^2")
        # 人口密度：中心高、边缘低，叠加随机扰动
        dist_ring = h3.grid_distance(center_cell, cell)
        decay = math.exp(-0.18 * dist_ring)
        population = int(rng.gauss(3200 * decay + 400, 260))
        population = max(population, 60)

        wkt_4326, wkt_3857 = _cell_polygons(cell)
        grids.append(
            {
                "h3_idx": cell,
                "h3_res": h3.get_resolution(cell),
                "parent_idx": h3.cell_to_parent(cell, max(res - 1, 0)),
                "district_code": DISTRICT_CODE,
                "area_km2": round(area, 6),
                "population": population,
                "geom": wkt_4326,
                "geom_3857": wkt_3857,
                "srid_source": "GCJ-02",
            }
        )
    return grids


def build_events(
    rng: random.Random,
    grids: list[dict[str, Any]],
    total: int,
) -> list[dict[str, Any]]:
    """生成事件工单。

    分布规则：15% 网格为热点（权重 ×4），叠加季度性与时段性权重。
    这样"上周占道经营 Top5 网格"才有稳定的答案，评测才有真值。
    """
    hotspots = set(rng.sample([g["h3_idx"] for g in grids], max(1, len(grids) // 7)))
    grid_weights = [
        (g["h3_idx"], 4.0 if g["h3_idx"] in hotspots else 1.0) for g in grids
    ]

    month_weights = []
    for month_offset in range(12):
        month = ((WINDOW_START.month - 1 + month_offset) % 12) + 1
        month_weights.append(1.0 + 0.35 * math.sin((month - 3) / 12 * 2 * math.pi))

    total_span = int((WINDOW_END - WINDOW_START).total_seconds())
    # 抽样权重预先拆成两个列表，避免在 24000 次循环里反复重建
    grid_ids = [cell for cell, _weight in grid_weights]
    grid_w = [weight for _cell, weight in grid_weights]
    events: list[dict[str, Any]] = []

    for index in range(total):
        # 月份按季节权重抽样，再在月内均匀取时刻
        month_offset = rng.choices(range(12), weights=month_weights, k=1)[0]
        month_start = WINDOW_START + timedelta(days=30.5 * month_offset)
        reported = month_start + timedelta(seconds=rng.randrange(30 * 24 * 3600))
        if not (WINDOW_START <= reported < WINDOW_END):
            reported = WINDOW_START + timedelta(seconds=rng.randrange(total_span))

        # 时段修正：按小时权重以 60% 概率重抽，保留一部分随机性
        if rng.random() < 0.6:
            hour = rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]
            reported = reported.replace(hour=hour, minute=rng.randrange(60))

        month = reported.month
        cat_weights = []
        for code, _name, weight, _hours, _sev in CATEGORIES:
            cat_weights.append(weight * SEASONAL.get(month, {}).get(code, 1.0))
        category = rng.choices(CATEGORIES, weights=cat_weights, k=1)[0]

        h3_idx = rng.choices(grid_ids, weights=grid_w, k=1)[0]
        lat, lng = _jitter_in_cell(rng, h3_idx)

        status = rng.choices(
            ["closed", "dispatched", "pending"], weights=[0.85, 0.11, 0.04], k=1
        )[0]
        closed_at = None
        if status == "closed":
            hours = category[3] * rng.uniform(0.15, 1.1)
            closed_at = reported + timedelta(hours=hours)
            if closed_at >= datetime(2026, 12, 1, tzinfo=CST):
                closed_at = None
                status = "dispatched"

        events.append(
            {
                "event_id": index + 1,
                "category_code": category[0],
                "h3_idx": h3_idx,
                "district_code": DISTRICT_CODE,
                "reported_at": reported.isoformat(timespec="seconds"),
                "closed_at": closed_at.isoformat(timespec="seconds") if closed_at else "",
                "status": status,
                "geom": _point_wkt(lat, lng),
                "source": "12345" if rng.random() < 0.72 else "chengguan",
            }
        )
    return events


def build_pois(rng: random.Random, grids: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    pois: list[dict[str, Any]] = []
    cells = [g["h3_idx"] for g in grids]
    for index in range(count):
        poi_type, name, capacity = rng.choice(POI_TYPES)
        cell = rng.choice(cells)
        lat, lng = _jitter_in_cell(rng, cell)
        pois.append(
            {
                "poi_id": f"P{index + 1:04d}",
                "name": f"{name}{index + 1:03d}",
                "poi_type": poi_type,
                "h3_idx": cell,
                "geom": _point_wkt(lat, lng),
                "capacity": capacity if capacity else "",
            }
        )
    return pois


def build_resources(
    rng: random.Random, grids: list[dict[str, Any]], count: int
) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    cells = [g["h3_idx"] for g in grids]
    for index in range(count):
        res_type, name = rng.choice(RESOURCE_TYPES)
        cell = rng.choice(cells)
        lat, lng = _jitter_in_cell(rng, cell)
        resources.append(
            {
                "resource_id": f"R{index + 1:03d}",
                "name": f"{name}{index + 1:02d}",
                "resource_type": res_type,
                "h3_idx": cell,
                "geom": _point_wkt(lat, lng),
                "staff_count": rng.randrange(6, 30),
            }
        )
    return resources


# ---- 落盘 --------------------------------------------------------------------


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(
    out_dir: Path,
    *,
    seed: int = 42,
    center_lat: float = 39.9219,
    center_lng: float = 116.4435,
    res: int = 8,
    ring: int = 8,
    events: int = 24000,
    pois: int = 300,
    resources: int = 40,
) -> dict[str, Any]:
    """生成全套 S1 数据，返回 manifest。同 seed 两次调用输出完全一致。"""
    rng = random.Random(seed)
    grids = build_grid(rng, center_lat, center_lng, res, ring)
    event_rows = build_events(rng, grids, events)
    poi_rows = build_pois(rng, grids, pois)
    resource_rows = build_resources(rng, grids, resources)

    category_rows = [
        {
            "category_code": code,
            "name": name,
            "handling_hours": hours,
            "severity": severity,
        }
        for code, name, _w, hours, severity in CATEGORIES
    ]

    district_rows = [
        {
            "district_code": DISTRICT_CODE,
            "name": DISTRICT_NAME,
            "geom": _district_wkt([g["h3_idx"] for g in grids]),
            "area_km2": round(sum(g["area_km2"] for g in grids), 6),
            "population": sum(g["population"] for g in grids),
        }
    ]

    out_dir = Path(out_dir)
    bundles = [
        ("dim_district.csv", district_rows,
         ["district_code", "name", "geom", "area_km2", "population"]),
        ("dim_grid.csv", grids,
         ["h3_idx", "h3_res", "parent_idx", "district_code", "area_km2",
          "population", "geom", "geom_3857", "srid_source"]),
        ("dim_category.csv", category_rows,
         ["category_code", "name", "handling_hours", "severity"]),
        ("dim_poi.csv", poi_rows,
         ["poi_id", "name", "poi_type", "h3_idx", "geom", "capacity"]),
        ("dim_resource.csv", resource_rows,
         ["resource_id", "name", "resource_type", "h3_idx", "geom", "staff_count"]),
        ("fact_event.csv", event_rows,
         ["event_id", "category_code", "h3_idx", "district_code", "reported_at",
          "closed_at", "status", "geom", "source"]),
    ]

    digests: dict[str, str] = {}
    counts: dict[str, int] = {}
    for name, rows, columns in bundles:
        digests[name] = _write_csv(out_dir / name, rows, columns)
        counts[name] = len(rows)

    combined = hashlib.sha256(
        "".join(f"{name}:{digest}" for name, digest in sorted(digests.items())).encode()
    ).hexdigest()[:16]

    manifest = {
        "seed": seed,
        "data_version": f"synth-{combined}",
        "center": {"lat": center_lat, "lng": center_lng},
        "h3_res": res,
        "ring": ring,
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "srid": "GCJ-02",
        "counts": counts,
        "digests": digests,
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 S1 场景合成数据")
    parser.add_argument("--out", default="data/generated", help="输出目录")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--res", type=int, default=8)
    parser.add_argument("--ring", type=int, default=8)
    parser.add_argument("--events", type=int, default=24000)
    parser.add_argument("--pois", type=int, default=300)
    parser.add_argument("--resources", type=int, default=40)
    args = parser.parse_args()

    manifest = generate(
        Path(args.out),
        seed=args.seed,
        res=args.res,
        ring=args.ring,
        events=args.events,
        pois=args.pois,
        resources=args.resources,
    )
    print(f"数据版本: {manifest['data_version']}")
    for name, count in manifest["counts"].items():
        print(f"  {name:<20} {count:>7} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
