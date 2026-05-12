-- 084_correlation_adjustments.sql
-- Phase 2G DRY-RUN sidecar log: for each cycle, captures the
-- correlation-adjusted sizing that WOULD have been submitted alongside
-- the production sizing. Operator parity-diffs production vs adjusted
-- until satisfied, then flips OPENCLAW_CORRELATION_ADJUSTED_LIVE=1 to
-- make it the production path.

CREATE TABLE IF NOT EXISTS correlation_adjustments (
    id                  BIGSERIAL    PRIMARY KEY,
    computed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    cycle_id            TEXT,                        -- correlate with parity_orders / alpaca_submissions cycle key
    ticker              TEXT         NOT NULL,
    production_qty      NUMERIC,
    production_notional NUMERIC,
    direction           TEXT,                         -- 'LONG' | 'SHORT'
    portfolio_kelly_phi NUMERIC,                      -- downsize factor in [0, 1.0]; 1.0 = no change
    adjusted_qty        NUMERIC,
    adjusted_notional   NUMERIC,
    correlation_input   JSONB                         -- Σ_effective slice + neighbor list (for audit)
);

CREATE INDEX IF NOT EXISTS idx_corr_adj_cycle
    ON correlation_adjustments (cycle_id, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_corr_adj_ticker
    ON correlation_adjustments (ticker, computed_at DESC);
