-- OUE-discount columns on strategy_weights_by_regime.
--
-- Operator (2026-05-16) approved auto-discounting strategy weights by
-- per-regime OUE health. Sunday weight cron (strategy_weights.py
-- rebuild()) now:
--   1. Counts O/U/E per (strategy_id, regime_state) from execution_signals.
--   2. Computes a multiplier in [0.2, 2.0] based on outlier bias.
--   3. Applies it before normalizing → strategies with high under_rate
--      get less capital, high over_rate get more.
--
-- We persist all four numbers per-row so the dashboard, comprehensive_review,
-- and the operator can see exactly what was applied and why.

ALTER TABLE strategy_weights_by_regime
  ADD COLUMN IF NOT EXISTS oue_over             INT,
  ADD COLUMN IF NOT EXISTS oue_under            INT,
  ADD COLUMN IF NOT EXISTS oue_expected         INT,
  ADD COLUMN IF NOT EXISTS oue_multiplier       NUMERIC,
  ADD COLUMN IF NOT EXISTS oue_adjusted_sharpe  NUMERIC;
