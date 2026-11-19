-- =============================================================================
-- 城市治理时空智能体 · 业务库 Schema（S1 场景：12345 / 城管事件时空态势研判）
-- =============================================================================
-- 坐标基准：全库统一 GCJ-02（与高德一致）。SRID 4326 承载经纬度用于展示，
-- 3857 投影列专用于面积与距离计算——这是"坐标基准显式契约"的落地：
-- 每个几何列都有明确的基准归属，不存在"隐式约定"。
--
-- H3 作为跨源统一空间主键：所有事实表都带 h3_idx，跨源聚合退化为等值 join，
-- 避免让 LLM 生成带拓扑判断的空间 SQL。
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS citygov;
SET search_path TO citygov, public;

CREATE EXTENSION IF NOT EXISTS postgis;

-- -----------------------------------------------------------------------------
-- 维表
-- -----------------------------------------------------------------------------

-- 行政区
CREATE TABLE dim_district (
    district_code   text PRIMARY KEY,
    name            text NOT NULL,
    geom            geometry(MultiPolygon, 4326) NOT NULL,
    area_km2        numeric(12, 6) NOT NULL CHECK (area_km2 > 0),
    population      integer NOT NULL DEFAULT 0 CHECK (population >= 0)
);

COMMENT ON TABLE dim_district IS '行政区维表，几何为 GCJ-02 基准';

-- H3 网格（默认 res 8，约 0.7 km²/格）
CREATE TABLE dim_grid (
    h3_idx          char(15) PRIMARY KEY,
    h3_res          smallint NOT NULL DEFAULT 8 CHECK (h3_res BETWEEN 5 AND 12),
    parent_idx      char(15) NOT NULL,
    district_code   text NOT NULL REFERENCES dim_district(district_code),
    area_km2        numeric(10, 6) NOT NULL CHECK (area_km2 > 0),
    population      integer NOT NULL DEFAULT 0 CHECK (population >= 0),
    geom            geometry(Polygon, 4326) NOT NULL,
    geom_3857       geometry(Polygon, 3857) NOT NULL,
    srid_source     text NOT NULL DEFAULT 'GCJ-02'
);

CREATE INDEX idx_dim_grid_parent ON dim_grid (parent_idx);
CREATE INDEX idx_dim_grid_geom ON dim_grid USING gist (geom);

COMMENT ON COLUMN dim_grid.srid_source IS '几何采集/生成时的坐标基准，进系统时必须显式声明';

-- 事件分类与处置时限
CREATE TABLE dim_category (
    category_code   text PRIMARY KEY,
    name            text NOT NULL,
    handling_hours  smallint NOT NULL CHECK (handling_hours > 0),
    severity        text NOT NULL CHECK (severity IN ('low', 'medium', 'high'))
);

-- 公共服务设施（POI）
CREATE TABLE dim_poi (
    poi_id          text PRIMARY KEY,
    name            text NOT NULL,
    poi_type        text NOT NULL,
    h3_idx          char(15) NOT NULL REFERENCES dim_grid(h3_idx),
    geom            geometry(Point, 4326) NOT NULL,
    capacity        integer CHECK (capacity IS NULL OR capacity >= 0)
);

CREATE INDEX idx_dim_poi_h3 ON dim_poi (h3_idx);
CREATE INDEX idx_dim_poi_geom ON dim_poi USING gist (geom);

-- 处置力量（城管队 / 环卫站 / 巡查组）
CREATE TABLE dim_resource (
    resource_id     text PRIMARY KEY,
    name            text NOT NULL,
    resource_type   text NOT NULL,
    h3_idx          char(15) NOT NULL REFERENCES dim_grid(h3_idx),
    geom            geometry(Point, 4326) NOT NULL,
    staff_count     smallint NOT NULL DEFAULT 0 CHECK (staff_count >= 0)
);

CREATE INDEX idx_dim_resource_h3 ON dim_resource (h3_idx);

-- -----------------------------------------------------------------------------
-- 事实表：事件工单（按月分区，12 个月）
-- -----------------------------------------------------------------------------

CREATE TABLE fact_event (
    event_id        bigserial,
    category_code   text NOT NULL REFERENCES dim_category(category_code),
    h3_idx          char(15) NOT NULL REFERENCES dim_grid(h3_idx),
    district_code   text NOT NULL REFERENCES dim_district(district_code),
    reported_at     timestamptz NOT NULL,
    closed_at       timestamptz,
    status          text NOT NULL CHECK (status IN ('pending', 'dispatched', 'closed', 'rejected')),
    geom            geometry(Point, 4326) NOT NULL,
    source          text NOT NULL DEFAULT '12345',
    PRIMARY KEY (event_id, reported_at),
    CHECK (closed_at IS NULL OR closed_at >= reported_at)
) PARTITION BY RANGE (reported_at);

CREATE INDEX idx_fact_event_h3 ON fact_event (h3_idx, reported_at);
CREATE INDEX idx_fact_event_cat ON fact_event (category_code, reported_at);
CREATE INDEX idx_fact_event_geom ON fact_event USING gist (geom);

-- 数据窗口：2025-12-01 ~ 2026-11-30（12 个自然月）
CREATE TABLE fact_event_2025_12 PARTITION OF fact_event
    FOR VALUES FROM ('2025-12-01') TO ('2026-01-01');
CREATE TABLE fact_event_2026_01 PARTITION OF fact_event
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE fact_event_2026_02 PARTITION OF fact_event
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE fact_event_2026_03 PARTITION OF fact_event
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
CREATE TABLE fact_event_2026_04 PARTITION OF fact_event
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
CREATE TABLE fact_event_2026_05 PARTITION OF fact_event
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE fact_event_2026_06 PARTITION OF fact_event
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE fact_event_2026_07 PARTITION OF fact_event
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
CREATE TABLE fact_event_2026_08 PARTITION OF fact_event
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE fact_event_2026_09 PARTITION OF fact_event
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE fact_event_2026_10 PARTITION OF fact_event
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE fact_event_2026_11 PARTITION OF fact_event
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');

-- -----------------------------------------------------------------------------
-- 指标语义层：把业务术语映射为确定性计算定义（LLM 只做槽位填充，不写 SQL）
-- -----------------------------------------------------------------------------

CREATE TABLE metric_dict (
    metric_code     text PRIMARY KEY,
    display_name    text NOT NULL,
    definition      text NOT NULL,
    formula_sql     text NOT NULL,
    unit            text,
    grain           text NOT NULL DEFAULT 'h3_res8',
    requires_columns text[] NOT NULL DEFAULT '{}'
);

COMMENT ON TABLE metric_dict IS '指标语义层。formula_sql 为参数化模板，占位符由 Planner 填充';
