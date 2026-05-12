-- 081_strategy_regime_mc_runs.sql
-- Cached bootstrap Monte Carlo results for size_scalar proposals.
-- Phase 2D: persisted so the dashboard can re-render CI panels without
-- re-bootstrapping on every load.

CREATE TABLE IF NOT EXISTS strategy_regime_mc_runs (
    id                BIGSERIAL    PRIMARY KEY,
    run_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    strategy_id       TEXT         NOT NULL,
    regime_state      TEXT         NOT NULL,
    current_size      NUMERIC,
    proposed_size     NUMERIC,
    n_trades_sampled  INTEGER      NOT NULL,
    n_bootstrap_iter  INTEGER      NOT NULL,
    sharpe_p05        NUMERIC,
    sharpe_p50        NUMERIC,
    sharpe_p95        NUMERIC,
    mean_pnl_p05      NUMERIC,
    mean_pnl_p50      NUMERIC,
    mean_pnl_p95      NUMERIC,
    max_dd_p05        NUMERIC,
    max_dd_p50        NUMERIC,
    max_dd_p95        NUMERIC,
    proposal_id       BIGINT
);

CREATE INDEX IF NOT EXISTS idx_srmcr_strategy_regime
    ON strategy_regime_mc_runs (strategy_id, regime_state, run_at DESC);
