-- 063_drop_backtest_method.sql
--
-- Drop strategy_registry.backtest_method. The v1/v2 split was introduced
-- 2026-04-27 to flag pre-regime-stratified rows during the rollout.
-- After backfill_candidate_metrics.py --method-filter v1 refreshed the
-- candidate-state rows, we cleared every remaining v1 row's metrics
-- (NULL'd sharpe/dd/return/trade_count/breakdown) and removed the v1/v2
-- badge from the dashboard. The column has no readers left.
--
-- backtest_regime_breakdown stays — the dashboard tooltip still uses it.

ALTER TABLE strategy_registry
  DROP COLUMN IF EXISTS backtest_method;
