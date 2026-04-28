-- 058_saturday_brain.sql
-- Schema for the consolidated Saturday research run ("saturday brain").
--
-- The Saturday brain replaces the split Sat-corpus + Sun-paper-expansion timers
-- with a single 10am-ET orchestrator (`src/agent/curators/saturday_brain.js`)
-- that ingests, rates, paperhunter-fans, tiers by data availability, and
-- synchronously codes Tier-A strategies. Tier-B candidates land in STAGING
-- waiting for an operator's data-fetch approval click; Tier-C candidates are
-- noted in the vault as future provider-unlock candidates.
--
-- Columns added here:
--   curated_candidates.implementability_score  Opus's score for "is the recipe
--                                              concrete enough that StrategyCoder
--                                              could turn it into Python".
--   curated_candidates.data_requirements_hint  Opus-inferred required columns;
--                                              refined later by paperhunter.
--   research_candidates.data_tier              'A' | 'B' | 'C' assigned in
--                                              Phase 5 of the brain.
--   strategy_registry.data_requirements_planned   For Tier-B staging entries:
--                                                 the missing-data spec the
--                                                 dashboard renders + the
--                                                 staging_approver enqueues
--                                                 once the operator approves.
--   strategy_registry.staging_approved_at         Operator's STAGING -> CANDIDATE
--                                                 click. NULL until clicked,
--                                                 populated by staging_approver.
--                                                 Gates the post-fetch trigger.
-- New table:
--   saturday_runs       One row per Saturday-brain invocation. Tracks phase
--                       progress, totals, and total cost so the dashboard /
--                       Discord summary can report.

CREATE TABLE IF NOT EXISTS saturday_runs (
    run_id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    status              TEXT         NOT NULL DEFAULT 'running',
                                     -- running|completed|failed|partial
    current_phase       TEXT,        -- 'preflight'|'expand'|'sweep'|'rate'|
                                     -- 'hunt'|'tier'|'code'|'ideate'|'stage'|
                                     -- 'vault'|'closeout'
    context_snapshot    JSONB,       -- Phase 0 capability_map + manifest snapshot
    -- Phase totals
    sources_discovered  INT          DEFAULT 0,
    papers_ingested     INT          DEFAULT 0,
    papers_rated        INT          DEFAULT 0,
    implementable_n     INT          DEFAULT 0,
    paperhunters_run    INT          DEFAULT 0,
    tier_a_count        INT          DEFAULT 0,
    tier_b_count        INT          DEFAULT 0,
    tier_c_count        INT          DEFAULT 0,
    coded_synchronous   INT          DEFAULT 0,   -- Tier-A successful
    coded_failed        INT          DEFAULT 0,   -- Tier-A backtest/code fail
    cost_usd            NUMERIC(10,4) DEFAULT 0,
    error_detail        TEXT
);

CREATE INDEX IF NOT EXISTS idx_saturday_runs_started ON saturday_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_saturday_runs_status  ON saturday_runs(status);

-- ── curated_candidates additions ─────────────────────────────────────────────
-- Opus's per-paper implementability score. 0.000–1.000. NULL on legacy rows
-- before this migration; the corpus prompt assigns it from Phase 3 onwards.
ALTER TABLE curated_candidates
    ADD COLUMN IF NOT EXISTS implementability_score NUMERIC(4,3);

-- Opus-inferred data needs from abstract — sketchy but useful for early
-- triage before paperhunter runs the full extraction. Shape:
--   {"required": ["prices", "options_iv"], "optional": ["filings"]}
ALTER TABLE curated_candidates
    ADD COLUMN IF NOT EXISTS data_requirements_hint JSONB;

CREATE INDEX IF NOT EXISTS idx_curated_candidates_impl
    ON curated_candidates(implementability_score DESC NULLS LAST);

-- ── research_candidates additions ────────────────────────────────────────────
-- Tier label written by data_tier_filter.tierCandidate() after paperhunter
-- has populated hunter_result_json. Single char keeps indexes tight.
ALTER TABLE research_candidates
    ADD COLUMN IF NOT EXISTS data_tier CHAR(1)
    CHECK (data_tier IN ('A','B','C') OR data_tier IS NULL);

CREATE INDEX IF NOT EXISTS idx_research_candidates_data_tier
    ON research_candidates(data_tier) WHERE data_tier IS NOT NULL;

-- ── strategy_registry additions ──────────────────────────────────────────────
-- Tier-B: the planned data fetches the dashboard renders and the staging
-- approver enqueues. Shape mirrors data_ingestion_queue keys:
--   [{provider:'fmp', column:'options_iv', ticker_set:[...], date_range:[from,to]}]
ALTER TABLE strategy_registry
    ADD COLUMN IF NOT EXISTS data_requirements_planned JSONB;

-- Operator's STAGING -> CANDIDATE click. NULL while the strategy waits in
-- staging; populated by staging_approver when the operator approves the data
-- fetch. data-task-executor's post-fetch trigger only acts on rows where this
-- is non-null.
ALTER TABLE strategy_registry
    ADD COLUMN IF NOT EXISTS staging_approved_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_strategy_registry_staging
    ON strategy_registry(staging_approved_at)
    WHERE staging_approved_at IS NOT NULL;
