-- 079_strategy_regime_priors.sql
-- Per-(strategy, regime) expected performance priors. Sparse table:
-- populated only for (strategy, regime) pairs where research or operator
-- has set a baseline. Unpopulated rows mean "no prior; drift compares
-- against the most recent applied proposal instead". Phase 2C.

CREATE TABLE IF NOT EXISTS strategy_regime_priors (
    strategy_id          TEXT         NOT NULL,
    regime_state         TEXT         NOT NULL,   -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    expected_sharpe      NUMERIC,                  -- annualized, post-fee
    expected_win_rate    NUMERIC,                  -- 0.0 - 1.0
    expected_avg_pnl_pct NUMERIC,
    source               TEXT         NOT NULL,   -- e.g. 'Asness 2013', 'Mastermind:2026-05-12', 'operator:internal-backtest'
    confidence           NUMERIC,                  -- 0.0 - 1.0
    notes                TEXT,
    set_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    set_by               TEXT         NOT NULL,
    PRIMARY KEY (strategy_id, regime_state)
);
