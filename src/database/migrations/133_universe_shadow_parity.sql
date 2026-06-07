-- 133: universe_shadow_parity (SP-7 Phase C C1 shadow, 2026-06-07)
--
-- One row per (run_date, strategy_id): the resolver-built per-strategy
-- universe diffed against the actual clamped universe the engine used.
-- Parity criterion (spec §3.5): zero diff for all is_adopted=FALSE rows on
-- ≥3 consecutive trading days. resolve_error non-NULL = the builder
-- failed-open for that strategy (counts as a parity break — code, not data).

CREATE TABLE IF NOT EXISTS universe_shadow_parity (
    id              BIGSERIAL    PRIMARY KEY,
    run_date        DATE         NOT NULL,
    strategy_id     TEXT         NOT NULL,
    predicate       TEXT         NOT NULL,             -- e.g. 'sp500', 'tier_r1000'
    n_resolved      INT          NOT NULL,
    n_actual        INT          NOT NULL,
    added_tickers   JSONB        NOT NULL DEFAULT '[]',  -- resolved − actual
    removed_tickers JSONB        NOT NULL DEFAULT '[]',  -- actual − resolved
    is_adopted      BOOLEAN      NOT NULL DEFAULT FALSE,
    resolve_error   TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (run_date, strategy_id)
);

CREATE INDEX IF NOT EXISTS idx_usp_run_date
    ON universe_shadow_parity (run_date DESC);
