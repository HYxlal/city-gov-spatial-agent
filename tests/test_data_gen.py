"""合成数据生成器的契约测试。

数据是评测的地基：如果同一 seed 两次生成结果不同，真值与实验都无法复现；
如果 H3 索引或坐标列写错，后续所有空间结论都是错的。这里守住这几条底线。
"""

from __future__ import annotations

import csv
import importlib.util
import math
import re
from datetime import datetime
from pathlib import Path

import h3
import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "generate_synthetic_data", ROOT / "data" / "generate_synthetic_data.py"
)
assert _spec and _spec.loader
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)

POINT_RE = re.compile(r"^POINT\(-?\d+\.\d+ -?\d+\.\d+\)$")
POLYGON_RE = re.compile(r"^POLYGON\(\(.+\)\)$")


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("gen")
    generator.generate(out, seed=7, ring=3, events=400, pois=30, resources=8)
    return out


class TestReproducibility:
    def test_same_seed_same_digest(self, tmp_path) -> None:
        first = generator.generate(tmp_path / "a", seed=11, ring=2, events=150,
                                   pois=10, resources=4)
        second = generator.generate(tmp_path / "b", seed=11, ring=2, events=150,
                                    pois=10, resources=4)
        assert first["digests"] == second["digests"]
        assert first["data_version"] == second["data_version"]

    def test_different_seed_differs(self, tmp_path) -> None:
        first = generator.generate(tmp_path / "a", seed=1, ring=2, events=150,
                                   pois=10, resources=4)
        second = generator.generate(tmp_path / "b", seed=2, ring=2, events=150,
                                    pois=10, resources=4)
        assert first["digests"]["fact_event.csv"] != second["digests"]["fact_event.csv"]

    def test_manifest_version_tracks_content(self, dataset: Path) -> None:
        import json

        manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["data_version"].startswith("synth-")
        assert manifest["srid"] == "GCJ-02"
        assert set(manifest["digests"]) >= {"dim_grid.csv", "fact_event.csv"}


class TestGrid:
    def test_h3_indexes_valid(self, dataset: Path) -> None:
        rows = _read(dataset / "dim_grid.csv")
        assert rows
        for row in rows:
            assert h3.is_valid_cell(row["h3_idx"]), row["h3_idx"]
            assert len(row["h3_idx"]) == 15

    def test_parent_is_one_resolution_up(self, dataset: Path) -> None:
        for row in _read(dataset / "dim_grid.csv"):
            assert h3.get_resolution(row["parent_idx"]) == int(row["h3_res"]) - 1

    def test_two_coordinate_systems_are_distinct(self, dataset: Path) -> None:
        """3857 列必须是米制坐标，不能把经纬度直接抄过去。"""
        for row in _read(dataset / "dim_grid.csv"):
            assert POLYGON_RE.match(row["geom"]), row["geom"][:40]
            assert POLYGON_RE.match(row["geom_3857"]), row["geom_3857"][:40]
            assert row["geom"] != row["geom_3857"]

            first_x = float(row["geom_3857"].split("((")[1].split(" ")[0])
            assert 1.2e7 < first_x < 1.4e7, "3857 的 x 应在 12000 km 量级"

    def test_coordinate_basis_declared(self, dataset: Path) -> None:
        assert all(r["srid_source"] == "GCJ-02" for r in _read(dataset / "dim_grid.csv"))

    def test_area_and_population_positive(self, dataset: Path) -> None:
        for row in _read(dataset / "dim_grid.csv"):
            assert float(row["area_km2"]) > 0
            assert int(row["population"]) > 0


class TestEvents:
    def test_count_matches_request(self, dataset: Path) -> None:
        assert len(_read(dataset / "fact_event.csv")) == 400

    def test_time_window_respected(self, dataset: Path) -> None:
        start = datetime.fromisoformat(generator.WINDOW_START.isoformat())
        end = datetime.fromisoformat(generator.WINDOW_END.isoformat())
        for row in _read(dataset / "fact_event.csv"):
            reported = datetime.fromisoformat(row["reported_at"])
            assert start <= reported < end, row["reported_at"]

    def test_status_enum(self, dataset: Path) -> None:
        allowed = {"pending", "dispatched", "closed", "rejected"}
        for row in _read(dataset / "fact_event.csv"):
            assert row["status"] in allowed
            if row["status"] == "closed":
                assert row["closed_at"], "已办结工单必须有办结时间"
                assert datetime.fromisoformat(row["closed_at"]) >= datetime.fromisoformat(
                    row["reported_at"]
                )

    def test_events_reference_existing_grid(self, dataset: Path) -> None:
        grids = {r["h3_idx"] for r in _read(dataset / "dim_grid.csv")}
        for row in _read(dataset / "fact_event.csv"):
            assert row["h3_idx"] in grids, "事件网格必须存在于维表"

    def test_event_geometry_is_point(self, dataset: Path) -> None:
        for row in _read(dataset / "fact_event.csv"):
            assert POINT_RE.match(row["geom"]), row["geom"]

    def test_categories_covered(self, dataset: Path) -> None:
        """400 条样本下应覆盖全部 6 类，否则评测集构造会偏向。"""
        seen = {r["category_code"] for r in _read(dataset / "fact_event.csv")}
        assert len(seen) == 6, f"实际类别: {sorted(seen)}"


class TestRoundTrip:
    def test_mercator_round_trip(self) -> None:
        """投影与反投影应能回到原点附近（误差 < 1e-6 度）。"""
        lat, lng = 39.9219, 116.4435
        x, y = generator._to_mercator(lat, lng)

        back_lng = x * 180.0 / 20037508.342789244
        back_lat = math.degrees(
            2 * math.atan(math.exp(y * math.pi / 20037508.342789244)) - math.pi / 2
        )
        assert back_lat == pytest.approx(lat, abs=1e-6)
        assert back_lng == pytest.approx(lng, abs=1e-6)
