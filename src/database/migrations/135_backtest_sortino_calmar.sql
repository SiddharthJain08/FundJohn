-- 135_backtest_sortino_calmar.sql
-- Persist sortino/calmar/avg_pnl that aggregate_metrics already computes but
-- the strategy_backtest_runs/_regimes INSERTs drop. Additive columns only.
ALTER TABLE strategy_backtest_runs
  ADD COLUMN IF NOT EXISTS total_sortino     NUMERIC,
  ADD COLUMN IF NOT EXISTS total_calmar      NUMERIC,
  ADD COLUMN IF NOT EXISTS total_avg_pnl_pct NUMERIC;

ALTER TABLE strategy_backtest_regimes
  ADD COLUMN IF NOT EXISTS sortino NUMERIC,
  ADD COLUMN IF NOT EXISTS calmar  NUMERIC;
