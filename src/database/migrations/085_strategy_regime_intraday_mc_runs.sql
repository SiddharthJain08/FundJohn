-- 085_strategy_regime_intraday_mc_runs.sql
-- Phase 2E: cached path-dependent Monte Carlo results for size/stop/target/
-- max-hold proposals. Mirrors 081 (`strategy_regime_mc_runs`, the linear MC)
-- but persists path-MC fields (path_source, exit-reason rates) so the
-- dashboard can render linear-vs-path side-by-side per proposal.
--
-- path_source values:
--   'empirical'  resampled real 30m bars (tickers with coverage)
--   'gbm'        synthetic GBM paths calibrated to daily realized vol
--   'hybrid'     mixed pool (cross-ticker strategy in regime, some have
--                30m bars, others don't)
--
-- Spec: docs/superpowers/specs/2026-05-13-regime-blended-sizer-phase-2e-design.md

CREATE TABLE IF NOT EXISTS strategy_regime_intraday_mc_runs (
    id                       BIGSERIAL    PRIMARY KEY,
    run_at                   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    strategy_id              TEXT         NOT NULL,
    regime_state             TEXT         NOT NULL,
    current_size             NUMERIC,
    proposed_size            NUMERIC,
    proposed_stop_pct        NUMERIC,
    proposed_target_pct      NUMERIC,
    proposed_max_hold_days   INTEGER,
    n_trades_sampled         INTEGER      NOT NULL,
    n_bootstrap_iter         INTEGER      NOT NULL,
    path_source              TEXT         NOT NULL,
    sharpe_p05               NUMERIC,
    sharpe_p50               NUMERIC,
    sharpe_p95               NUMERIC,
    mean_pnl_p05             NUMERIC,
    mean_pnl_p50             NUMERIC,
    mean_pnl_p95             NUMERIC,
    max_dd_p05               NUMERIC,
    max_dd_p50               NUMERIC,
    max_dd_p95               NUMERIC,
    stop_hit_rate            NUMERIC,
    target_hit_rate          NUMERIC,
    max_hold_hit_rate        NUMERIC,
    proposal_id              BIGINT
);

CREATE INDEX IF NOT EXISTS idx_srimcr_strategy_regime
    ON strategy_regime_intraday_mc_runs (strategy_id, regime_state, run_at DESC);

CREATE INDEX IF NOT EXISTS idx_srimcr_proposal
    ON strategy_regime_intraday_mc_runs (proposal_id)
 WHERE proposal_id IS NOT NULL;
