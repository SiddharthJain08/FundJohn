-- 112_strategy_universe_recommendations.sql
-- Master invariant: rows append-only; columns may be added.

CREATE TABLE IF NOT EXISTS strategy_universe_recommendations (
  id                BIGSERIAL PRIMARY KEY,
  strategy_id       TEXT NOT NULL,
  recommended_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  current_predicate TEXT,                          -- import path or 'default'
  candidate_predicate TEXT NOT NULL,               -- proposed import path or 'default'
  candidate_set_id  TEXT NOT NULL,                 -- which 12-slice version was tested
  backtest_summary  JSONB NOT NULL,                -- per-candidate {sharpe, max_dd, trades, mean_universe_size}
  rationale         TEXT,                          -- Opus chain-of-reasoning excerpt
  approved          BOOLEAN,                       -- NULL until operator decides
  approved_at       TIMESTAMPTZ,
  approved_by       TEXT,
  adopted           BOOLEAN NOT NULL DEFAULT FALSE,
  adopted_at        TIMESTAMPTZ,
  mastermind_cost_usd NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_universe_recs_pending ON strategy_universe_recommendations(strategy_id) WHERE approved IS NULL;
CREATE INDEX IF NOT EXISTS idx_universe_recs_strategy_date ON strategy_universe_recommendations(strategy_id, recommended_at DESC);
