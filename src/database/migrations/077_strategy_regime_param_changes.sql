-- 077_strategy_regime_param_changes.sql
-- Append-only audit of every write to strategy_regime_params. JSONB before/after
-- snapshots make rollback trivial. Replaces regime_eligibility_changes (which stays
-- read-only for historical reference).

CREATE TABLE IF NOT EXISTS strategy_regime_param_changes (
    id              BIGSERIAL    PRIMARY KEY,
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor           TEXT         NOT NULL,
    strategy_id     TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL,
    before_row      JSONB,                    -- full prior row; NULL on first set
    after_row       JSONB        NOT NULL,
    reason          TEXT,
    source          TEXT                       -- 'dashboard' | 'cli' | 'mastermind' | 'migration'
);

CREATE INDEX IF NOT EXISTS idx_srpc_strategy_regime_time
    ON strategy_regime_param_changes (strategy_id, regime_state, changed_at DESC);
