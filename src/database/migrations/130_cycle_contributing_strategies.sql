-- 2026-06-08: per-cycle, per-ticker correlation-gate contributing strategies.
-- The sizer (regime_blended_sizer) already computes, per ticker, the strategies
-- whose deflated cum_sharpe cleared the correlation gate. Persisting that set
-- lets the dashboard ticker-alpha show ONLY contributing strategies (not every
-- one of the up-to-~30 that ever traded the ticker). Written by
-- regime_blended_sizer_live each cycle; read latest-per-ticker by the
-- /api/portfolio/ticker-alpha endpoint (graceful fallback to all when absent).
CREATE TABLE IF NOT EXISTS cycle_contributing_strategies (
    run_date    DATE        NOT NULL,
    ticker      TEXT        NOT NULL,
    strategies  TEXT[]      NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_ccs_ticker_date
    ON cycle_contributing_strategies (ticker, run_date DESC);
