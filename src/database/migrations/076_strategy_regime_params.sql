-- 076_strategy_regime_params.sql
-- Per-(strategy, regime) parameter row. Source of truth for regime_gate.is_eligible
-- and (Phase 2B onwards) per-regime size/stop/target/max-hold overrides.
-- Migrates ownership of eligible_regimes OUT of manifest.json. Append-only by
-- convention: every write also lands an audit row in strategy_regime_param_changes.

CREATE TABLE IF NOT EXISTS strategy_regime_params (
    strategy_id     TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL,  -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    eligible        BOOLEAN      NOT NULL,
    size_scalar     NUMERIC,                 -- NULL = fall back to Phase 1 regime-only static scalar
    stop_pct        NUMERIC,                 -- NULL = inherit strategy-wide default (Phase 2B populates)
    target_pct      NUMERIC,                 -- NULL = inherit strategy-wide default
    max_hold_days   INTEGER,                 -- NULL = inherit strategy-wide default
    set_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    set_by          TEXT         NOT NULL,   -- 'migration:from-manifest-2026-05-12' | 'operator:<name>' | 'mastermind' (future)
    PRIMARY KEY (strategy_id, regime_state)
);
