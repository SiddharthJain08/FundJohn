-- 073_strategy_regime_backtests.sql
-- Snapshots of per-strategy per-regime auto_backtest results.
-- Powers the regime-blended walk-forward harness's gate B comparison.
-- Append-only; harness reads the latest run_id.

CREATE TABLE IF NOT EXISTS strategy_regime_backtests (
    run_id            UUID         NOT NULL,
    strategy_id       TEXT         NOT NULL,
    regime_state      TEXT         NOT NULL,  -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    sharpe            NUMERIC,
    max_dd            NUMERIC,
    total_return_pct  NUMERIC,
    trade_count       INTEGER,
    oos_days          INTEGER,
    window_count      INTEGER,
    note              TEXT,         -- 'not_declared' | 'no_oos_window' | NULL
    declared_regimes  TEXT[],
    period_start      DATE,
    period_end        DATE,
    run_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, strategy_id, regime_state)
);

CREATE INDEX IF NOT EXISTS idx_strb_strategy_regime
    ON strategy_regime_backtests (strategy_id, regime_state, run_at DESC);

CREATE INDEX IF NOT EXISTS idx_strb_latest
    ON strategy_regime_backtests (run_at DESC);
