-- 083_strategy_signal_overlap.sql
-- Nightly-computed pairwise overlap of strategy signals.
-- Phase 2D: data-layer foundation for future correlation-adjusted sizing.
-- Canonical ordering strategy_a < strategy_b so pairs aren't double-counted.

CREATE TABLE IF NOT EXISTS strategy_signal_overlap (
    computed_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    window_days     INTEGER      NOT NULL,
    strategy_a      TEXT         NOT NULL,
    strategy_b      TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL DEFAULT 'ANY',   -- 'ANY' = all-regimes aggregate row; specific regime label otherwise
    overlap_count   INTEGER      NOT NULL,
    a_signal_count  INTEGER      NOT NULL,
    b_signal_count  INTEGER      NOT NULL,
    jaccard_idx     NUMERIC,
    PRIMARY KEY (computed_at, window_days, strategy_a, strategy_b, regime_state)
);

CREATE INDEX IF NOT EXISTS idx_sso_strategy_a
    ON strategy_signal_overlap (strategy_a, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_sso_jaccard
    ON strategy_signal_overlap (jaccard_idx DESC, computed_at DESC);
