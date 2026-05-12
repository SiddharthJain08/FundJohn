-- 074_regime_live_pnl_rollup.sql
-- Per-strategy per-regime live PnL aggregates, computed nightly from
-- execution_signals × signal_pnl. Replaces backfill-based regime validation
-- with rolling live evidence. Append-only — new run_at row each night.
-- Phase 1 reads aggregate columns; Phase 2 (learned sizer) reads per-trade
-- detail joined back to signal_pnl by (strategy_id, regime_state, window_days).

CREATE TABLE IF NOT EXISTS strategy_regime_live_pnl_rollup (
    run_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    strategy_id     TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL,   -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    window_days     INTEGER      NOT NULL,   -- 30 | 90 | 0 (all-time)
    trade_count     INTEGER      NOT NULL,
    win_count       INTEGER      NOT NULL,
    total_pnl_pct   NUMERIC      NOT NULL,
    avg_pnl_pct     NUMERIC,
    stdev_pnl_pct   NUMERIC,
    sharpe_proxy    NUMERIC,                 -- avg/stdev * sqrt(252/avg_hold_days); informational
    max_dd_proxy    NUMERIC,                 -- worst single trade pnl_pct (negative)
    avg_hold_days   NUMERIC,
    last_signal_at  TIMESTAMPTZ,             -- newest signal_date in window
    PRIMARY KEY (run_at, strategy_id, regime_state, window_days)
);

CREATE INDEX IF NOT EXISTS idx_srlpr_latest
    ON strategy_regime_live_pnl_rollup (strategy_id, regime_state, window_days, run_at DESC);

CREATE INDEX IF NOT EXISTS idx_srlpr_run_at
    ON strategy_regime_live_pnl_rollup (run_at DESC);
