-- 075_regime_eligibility_audit.sql
-- Append-only history of operator-initiated changes to manifest
-- eligible_regimes. Every dashboard toggle and CLI mutation lands here.
-- This is the audit trail; manifest.json is the live state.

CREATE TABLE IF NOT EXISTS regime_eligibility_changes (
    id              SERIAL       PRIMARY KEY,
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor           TEXT         NOT NULL,    -- 'operator:<name>' | 'cli' | 'dashboard'
    strategy_id     TEXT         NOT NULL,
    before_regimes  TEXT[],
    after_regimes   TEXT[],
    reason          TEXT,
    source          TEXT                       -- e.g. 'live_sharpe_proxy=-0.4 over 90d'
);

CREATE INDEX IF NOT EXISTS idx_regime_eligibility_strategy_time
    ON regime_eligibility_changes (strategy_id, changed_at DESC);
