-- 090_strategy_weights_by_regime.sql
-- Versioned per-(strategy, regime) weight table populated by
-- src/execution/strategy_weights.py rebuild(). Older rows kept for audit;
-- is_current=TRUE marks the currently-active row consumed by the sizer.

CREATE TABLE IF NOT EXISTS strategy_weights_by_regime (
  id                BIGSERIAL PRIMARY KEY,
  strategy_id       TEXT NOT NULL,
  regime_state      TEXT NOT NULL,
  cadence_days      INTEGER NOT NULL,
  bt_sharpe         NUMERIC,
  bt_n              INTEGER,
  live_sharpe       NUMERIC,
  live_n            INTEGER,
  effective_sharpe  NUMERIC NOT NULL,
  weight            NUMERIC NOT NULL,
  daily_weight      NUMERIC NOT NULL,
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  trigger           TEXT NOT NULL,
  is_current        BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS strategy_weights_current_idx
  ON strategy_weights_by_regime (strategy_id, regime_state)
  WHERE is_current;
CREATE INDEX IF NOT EXISTS strategy_weights_regime_current_idx
  ON strategy_weights_by_regime (regime_state)
  WHERE is_current;
