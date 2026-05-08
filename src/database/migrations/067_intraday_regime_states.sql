-- 067_intraday_regime_states.sql
-- Append-only Postgres table for the intraday HMM regime detector.
--
-- One row per 5-min tick during RTH (Mon-Fri 9:30-16:00 ET, ~78 ticks/day).
-- Records: detected state, prior state (last persisted), HMM confidence,
-- the running hysteresis_streak (consecutive ticks in this state since the
-- last different state), whether this tick fired a liquidation, and the
-- transition tag if it did.
--
-- Discipline (per CLAUDE.md NEVER-DELETE invariant): rows are inserted on
-- every tick and never updated/deleted. The PRIMARY KEY on ts_utc gives
-- ON CONFLICT DO NOTHING semantics for replay-safety; cron drift or
-- detector restart never produces duplicate rows.
--
-- Storage: 78 rows/day × 252 days × 1 year ≈ 19,656 rows/year. Trivial.

CREATE TABLE IF NOT EXISTS intraday_regime_states (
    ts_utc              TIMESTAMPTZ PRIMARY KEY,
    state               TEXT        NOT NULL,
    prior_state         TEXT,
    confidence          REAL        NOT NULL,
    hysteresis_streak   INTEGER     NOT NULL DEFAULT 1,
    fired_liquidation   BOOLEAN     NOT NULL DEFAULT FALSE,
    transition_tag      TEXT,
    features_json       JSONB       NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-state ts-desc index for the hysteresis-lookback query
-- ("last N ticks in state X") and the dashboard timeline view.
CREATE INDEX IF NOT EXISTS idx_intraday_regime_state_ts
    ON intraday_regime_states (state, ts_utc DESC);

-- Fired-rows index for the daily rollup ("how many fires today by tag").
CREATE INDEX IF NOT EXISTS idx_intraday_regime_fired
    ON intraday_regime_states (fired_liquidation, ts_utc DESC)
 WHERE fired_liquidation = TRUE;
