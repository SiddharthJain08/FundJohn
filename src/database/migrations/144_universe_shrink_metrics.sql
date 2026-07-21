-- 144: universe ladder shrink metrics (campaign W3, 2026-07-21).
-- Per-(primary run, tier, regime) metrics derived by FILTERING the stored
-- full-universe strategy_backtest_trades down the tier ladder (no re-run) —
-- the same trades aggregated per sub-universe via aggregate_per_regime/
-- blend_metrics (pnl smear; sub-universe daily marks are not persisted).
-- regime_state='TOTAL' holds the day-frequency-blended grid metrics
-- (universe_grid_cli.blend_metrics convention — what select_tier compares);
-- canonical-regime rows hold the per-sleeve aggregates.
-- tier='full' is the unfiltered baseline (defines the maintain-constraint's
-- protected regime set). chosen=TRUE marks the tier the strategy actually
-- trades live (its adopted/current universe_filter_ref) — the activation
-- assigner and dashboard prefer chosen sleeves over the full-universe
-- strategy_backtest_regimes rows. Derived + recomputable: rows are upserted
-- per (run_id, tier, regime_state) on each shrink pass.

CREATE TABLE IF NOT EXISTS universe_shrink_metrics (
  id BIGSERIAL PRIMARY KEY,
  strategy_id TEXT NOT NULL,
  run_id UUID NOT NULL,
  tier TEXT NOT NULL,          -- 'full' | 'sp500' | 'tier_r1000' | 'tier_r3000' | 'tier_liquid'
  regime_state TEXT NOT NULL,  -- 'TOTAL' (blended) or a canonical regime
  sharpe DOUBLE PRECISION,
  max_dd_pct DOUBLE PRECISION,
  trade_count INTEGER,
  win_rate DOUBLE PRECISION,
  sortino DOUBLE PRECISION,
  calmar DOUBLE PRECISION,
  mean_holding_days DOUBLE PRECISION,
  return_pct DOUBLE PRECISION,
  chosen BOOLEAN NOT NULL DEFAULT FALSE,
  candidate_set_id TEXT,
  rec_id BIGINT,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (run_id, tier, regime_state)
);

CREATE INDEX IF NOT EXISTS idx_shrink_metrics_strategy
  ON universe_shrink_metrics(strategy_id, computed_at DESC);

CREATE INDEX IF NOT EXISTS idx_shrink_metrics_chosen
  ON universe_shrink_metrics(run_id) WHERE chosen;
