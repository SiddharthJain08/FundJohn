-- 118_crypto_regime_states.sql
-- Append-only Postgres table for the 24/7 crypto HMM regime detector (SP-3.1
-- Phase B). One row per hourly tick. Mirrors intraday_regime_states (067) with
-- fired_liquidation renamed to fired_redeploy (crypto fires redeploys, never
-- liquidations; stays FALSE in Phase B — Phase C sets it).
--
-- Discipline (CLAUDE.md NEVER-DELETE): rows inserted every tick, never
-- updated/deleted. PRIMARY KEY on ts_utc gives ON CONFLICT DO NOTHING replay
-- safety. Storage: 24 rows/day × 365 ≈ 8,760 rows/year. Trivial.

CREATE TABLE IF NOT EXISTS crypto_regime_states (
    ts_utc              TIMESTAMPTZ PRIMARY KEY,
    state               TEXT        NOT NULL,
    prior_state         TEXT,
    confidence          REAL        NOT NULL,
    hysteresis_streak   INTEGER     NOT NULL DEFAULT 1,
    fired_redeploy      BOOLEAN     NOT NULL DEFAULT FALSE,
    transition_tag      TEXT,
    features_json       JSONB       NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-state ts-desc index for the hysteresis-lookback query
-- ("last N ticks in state X") and the dashboard timeline view.
CREATE INDEX IF NOT EXISTS idx_crypto_regime_state_ts
    ON crypto_regime_states (state, ts_utc DESC);

-- Fired-rows index for the daily rollup ("how many redeploys today by tag").
CREATE INDEX IF NOT EXISTS idx_crypto_regime_fired
    ON crypto_regime_states (fired_redeploy, ts_utc DESC)
 WHERE fired_redeploy = TRUE;
