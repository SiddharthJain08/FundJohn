-- 080_strategy_regime_priors_changes.sql
-- Append-only audit of every write to strategy_regime_priors. Phase 2C.

CREATE TABLE IF NOT EXISTS strategy_regime_priors_changes (
    id              BIGSERIAL    PRIMARY KEY,
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor           TEXT         NOT NULL,
    strategy_id     TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL,
    before_row      JSONB,
    after_row       JSONB        NOT NULL,
    reason          TEXT
);

CREATE INDEX IF NOT EXISTS idx_srpr_changes_strategy_time
    ON strategy_regime_priors_changes (strategy_id, regime_state, changed_at DESC);
