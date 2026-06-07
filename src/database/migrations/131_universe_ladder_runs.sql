-- 131: universe_ladder_runs (SP-7 Phase B, 2026-06-06)
--
-- One row per (run_id, strategy_id, tier) ladder cell. The nightly queue
-- driver (scripts/run_universe_ladder.py) claims queued cells sequentially,
-- runs universe_grid_cli per cell, and writes terminal status + metrics here.
-- Resumability: cells stuck 'running' are reset to 'queued' at drain start.

CREATE TABLE IF NOT EXISTS universe_ladder_runs (
    id            BIGSERIAL    PRIMARY KEY,
    run_id        TEXT         NOT NULL,           -- e.g. 'ladder-20260608'
    strategy_id   TEXT         NOT NULL,
    tier          TEXT         NOT NULL,           -- sp500|tier_r1000|tier_r3000|tier_liquid
    status        TEXT         NOT NULL DEFAULT 'queued',
                  -- queued | running | done | timeout | error | skipped_degenerate
    window_start  DATE,
    window_end    DATE,
    artifact_path TEXT,
    metrics       JSONB,                           -- 8-key blend + trades_n etc.
    trade_sha     TEXT,
    duration_s    NUMERIC,
    stderr_tail   TEXT,
    queued_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    UNIQUE (run_id, strategy_id, tier)
);

CREATE INDEX IF NOT EXISTS idx_ulr_run_status
    ON universe_ladder_runs (run_id, status);

CREATE INDEX IF NOT EXISTS idx_ulr_strategy
    ON universe_ladder_runs (strategy_id, queued_at DESC);
