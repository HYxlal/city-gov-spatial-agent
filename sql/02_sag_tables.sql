-- =============================================================================
-- SAG 空间论证图持久化（五表 + 两个评测视图）
-- =============================================================================
-- 与 src/argument/sag.py 的 Pydantic 模型一一对应，字段名保持一致，
-- 便于 builder 落库、前端读库、评测脚本直接出数。
--
-- 设计取舍：
-- 1. 五张表，不把 Caveat 拆表——它挂在 Claim 上，用 jsonb 数组存储，
--    避免为"限定条件"引入联表复杂度；需要统计时用 v_sag_caveat 展开。
-- 2. 约束尽量下沉到数据库：证据类型枚举、digest 格式、no-evidence-no-claim
--    的基数约束都在 CHECK 里，防止绕过 Python 层直接写库污染评测数据。
-- 3. sag_claim.evidence_ids 无法用外键约束数组元素，由 v_sag_quality 检出
--    悬挂引用（写入端另有校验，这里是第二道防线）。
-- =============================================================================

SET search_path TO citygov, public;

-- -----------------------------------------------------------------------------
-- 1. 论证图主表：一次问答对应一张图
-- -----------------------------------------------------------------------------
CREATE TABLE sag_graph (
    sag_id          text PRIMARY KEY,
    thread_id       text NOT NULL,
    user_query      text NOT NULL,
    status          text NOT NULL DEFAULT 'built'
                    CHECK (status IN ('built', 'validated', 'rejected', 'replanned')),
    replan_count    smallint NOT NULL DEFAULT 0 CHECK (replan_count >= 0),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_sag_graph_thread ON sag_graph (thread_id, created_at DESC);

-- -----------------------------------------------------------------------------
-- 2. 数据血缘：保证证据的时空基准可信
-- -----------------------------------------------------------------------------
CREATE TABLE sag_provenance (
    provenance_id   text PRIMARY KEY,
    source_name     text NOT NULL CHECK (btrim(source_name) <> ''),
    time_window     text,
    srid            text NOT NULL DEFAULT 'GCJ-02',
    data_version    text NOT NULL,
    snapshot_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN sag_provenance.srid IS '数据源坐标基准声明。非 GCJ-02 时须在 Caveat 中标记';

-- -----------------------------------------------------------------------------
-- 3. 可执行算子：保证每条证据都能一键复算
-- -----------------------------------------------------------------------------
CREATE TABLE sag_operation (
    operation_id    text PRIMARY KEY,
    tool_name       text NOT NULL CHECK (btrim(tool_name) <> ''),
    template_or_sql text NOT NULL CHECK (btrim(template_or_sql) <> ''),
    params          jsonb NOT NULL DEFAULT '{}'::jsonb
                    CHECK (jsonb_typeof(params) = 'object'),
    -- What-if 复算的可调参数声明，前端据此渲染滑块
    adjustable_params jsonb NOT NULL DEFAULT '[]'::jsonb
                    CHECK (jsonb_typeof(adjustable_params) = 'array'),
    fallback_chain  jsonb NOT NULL DEFAULT '[]'::jsonb
                    CHECK (jsonb_typeof(fallback_chain) = 'array'),
    error_bound_m   numeric(10, 2) CHECK (error_bound_m IS NULL OR error_bound_m >= 0),
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- 4. 空间证据：限定四类
-- -----------------------------------------------------------------------------
CREATE TABLE sag_evidence (
    evidence_id     text PRIMARY KEY,
    sag_id          text NOT NULL REFERENCES sag_graph (sag_id) ON DELETE CASCADE,
    kind            text NOT NULL
                    CHECK (kind IN ('grid_agg', 'geom_overlay', 'point_set', 'comparison')),
    summary         text NOT NULL CHECK (btrim(summary) <> ''),
    raw_metrics     jsonb NOT NULL CHECK (jsonb_typeof(raw_metrics) = 'object'),
    layer_id        text,
    digest          text NOT NULL CHECK (digest LIKE 'sha256:%'),
    sample_size     integer CHECK (sample_size IS NULL OR sample_size >= 0),
    operation_id    text NOT NULL REFERENCES sag_operation (operation_id),
    provenance_id   text NOT NULL REFERENCES sag_provenance (provenance_id),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_sag_evidence_sag ON sag_evidence (sag_id);

-- -----------------------------------------------------------------------------
-- 5. 空间结论：no-evidence-no-claim 的数据库级兜底
-- -----------------------------------------------------------------------------
CREATE TABLE sag_claim (
    claim_id        text PRIMARY KEY,
    sag_id          text NOT NULL REFERENCES sag_graph (sag_id) ON DELETE CASCADE,
    thread_id       text NOT NULL,
    claim_text      text NOT NULL CHECK (btrim(claim_text) <> ''),
    -- 结构化数值声明（Metric 数组：raw / derived / external）
    metrics         jsonb NOT NULL DEFAULT '[]'::jsonb
                    CHECK (jsonb_typeof(metrics) = 'array'),
    extracted_entities jsonb NOT NULL DEFAULT '[]'::jsonb
                    CHECK (jsonb_typeof(extracted_entities) = 'array'),
    -- 结构硬约束：至少一条证据
    evidence_ids    text[] NOT NULL CHECK (cardinality(evidence_ids) >= 1),
    caveats         jsonb NOT NULL DEFAULT '[]'::jsonb
                    CHECK (jsonb_typeof(caveats) = 'array'),
    verified        boolean NOT NULL DEFAULT false,
    reject_reason   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_sag_claim_sag ON sag_claim (sag_id);
CREATE INDEX idx_sag_claim_thread ON sag_claim (thread_id, created_at DESC);
CREATE INDEX idx_sag_claim_verified ON sag_claim (verified) WHERE NOT verified;
-- 按指标名检索结论（如"找出所有提到 mom_pct 的结论"）
CREATE INDEX idx_sag_claim_metrics ON sag_claim USING gin (metrics jsonb_path_ops);

-- 被拒绝的结论必须写明原因，合法结论不应带原因
ALTER TABLE sag_claim ADD CONSTRAINT chk_reject_reason
    CHECK ((verified AND reject_reason IS NULL) OR (NOT verified));

-- -----------------------------------------------------------------------------
-- 视图 1：展开 Caveat，便于统计"降级标记覆盖率"
-- -----------------------------------------------------------------------------
CREATE VIEW v_sag_caveat AS
SELECT c.sag_id,
       c.claim_id,
       caveat ->> 'kind'      AS caveat_kind,
       caveat ->> 'message'   AS message,
       caveat ->> 'severity'  AS severity,
       (caveat ->> 'auto_generated')::boolean AS auto_generated,
       caveat ->> 'source_rule' AS source_rule
FROM sag_claim c
CROSS JOIN LATERAL jsonb_array_elements(c.caveats) AS caveat;

-- -----------------------------------------------------------------------------
-- 视图 2：逐条结论的质量体检（含悬挂证据引用检测）
-- -----------------------------------------------------------------------------
CREATE VIEW v_sag_quality AS
SELECT c.sag_id,
       c.thread_id,
       c.claim_id,
       c.verified,
       cardinality(c.evidence_ids)                        AS evidence_count,
       jsonb_array_length(c.caveats)                      AS caveat_count,
       jsonb_array_length(c.metrics)                      AS metric_count,
       -- 引用了不存在的证据：JSON 层抓不到的坏数据，这里能查出来
       EXISTS (
           SELECT 1
           FROM unnest(c.evidence_ids) AS eid
           LEFT JOIN sag_evidence e ON e.evidence_id = eid
           WHERE e.evidence_id IS NULL
       )                                                  AS has_dangling_evidence,
       c.created_at
FROM sag_claim c;

-- -----------------------------------------------------------------------------
-- 视图 3：全库指标汇总（论文评测可直接 SELECT 出数）
-- -----------------------------------------------------------------------------
CREATE VIEW v_sag_metric_summary AS
SELECT count(*)                                        AS total_claims,
       count(*) FILTER (WHERE verified)                AS verified_claims,
       count(DISTINCT sag_id)                          AS total_graphs,
       count(*) FILTER (WHERE has_dangling_evidence)   AS dangling_evidence_claims,
       round(avg(evidence_flag), 4)                    AS evidence_completeness,
       round(avg(verified_flag), 4)                    AS pass_rate
FROM (
    SELECT sag_id,
           verified,
           has_dangling_evidence,
           CASE WHEN evidence_count >= 1 THEN 1.0 ELSE 0.0 END AS evidence_flag,
           CASE WHEN verified THEN 1.0 ELSE 0.0 END            AS verified_flag
    FROM v_sag_quality
) AS q;
