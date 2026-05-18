-- Phase 2D — multi-source quote-monitor parity capture.
--
-- Append-only by CLAUDE.md invariant: this table grows for the duration of
-- the 5-day parity-observation window (and beyond), and never has rows
-- deleted. Each fan-out cycle writes N-1 rows per ticker comparing the
-- primary source (Polygon) against each other source that returned a
-- non-stale quote for the same ticker.
--
-- The operator reads this table to decide whether OPENCLAW_UNIFIED_QUOTES
-- can flip to '1' in production:
--   * divergence_bps < 5  on >99% of rows  → safe to flip
--   * any source consistently times out (run_at populated but no row from
--     that source in by_source view) → investigate that adapter first
--
-- divergence_pct = (price_b - price_a) / price_a * 100         (signed)
-- divergence_bps = round((price_b - price_a) / price_a * 1e4)  (signed int)

CREATE TABLE IF NOT EXISTS quote_monitor_parity (
    id                BIGSERIAL PRIMARY KEY,
    run_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker            TEXT        NOT NULL,
    source_a          TEXT        NOT NULL,      -- primary (Polygon)
    price_a           DOUBLE PRECISION NOT NULL,
    source_b          TEXT        NOT NULL,      -- comparison source
    price_b           DOUBLE PRECISION NOT NULL,
    divergence_pct    DOUBLE PRECISION NOT NULL, -- signed, % difference
    divergence_bps    INTEGER          NOT NULL, -- signed, basis points
    max_age_sec       DOUBLE PRECISION,          -- max(age_a, age_b) at compare time
    fetched_at_a      TIMESTAMPTZ,
    fetched_at_b      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_qm_parity_ticker_runat
    ON quote_monitor_parity (ticker, run_at DESC);
CREATE INDEX IF NOT EXISTS idx_qm_parity_source_b_runat
    ON quote_monitor_parity (source_b, run_at DESC);
CREATE INDEX IF NOT EXISTS idx_qm_parity_runat
    ON quote_monitor_parity (run_at DESC);
