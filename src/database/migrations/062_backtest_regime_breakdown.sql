-- 062_backtest_regime_breakdown.sql
--
-- Add regime-stratified backtest output to strategy_registry. The previous
-- backtest hardcoded a single regime for the entire run; the new pipeline
-- (auto_backtest.py v2) selects OOS windows per declared active_in_regime
-- and records per-regime sharpe/dd/return/trade_count alongside the
-- existing trade-weighted aggregates.
--
-- backtest_regime_breakdown JSONB shape:
--   {
--     "LOW_VOL":        {"sharpe": 1.20, "max_dd": 0.021, "total_return_pct": 4.4,
--                        "trade_count": 84,  "oos_days": 623},
--     "TRANSITIONING":  {...},
--     "HIGH_VOL":       {"note": "no_oos_window"},
--     "CRISIS":         {...}
--   }
--
-- backtest_method tracks which version of auto_backtest produced the row:
--   'v1_fixed_regime'      — pre-2026-04-27 single-fixed-regime run
--   'v2_regime_stratified' — current regime-stratified path
-- Existing rows pick up the v1 marker via the column default. New writes
-- by _codeFromQueue set v2 explicitly.
--
-- Idempotent.

ALTER TABLE strategy_registry
  ADD COLUMN IF NOT EXISTS backtest_regime_breakdown JSONB,
  ADD COLUMN IF NOT EXISTS backtest_method TEXT NOT NULL DEFAULT 'v1_fixed_regime';
