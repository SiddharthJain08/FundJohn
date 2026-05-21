-- Phase: per-regime sharpe-gate sliders on the Strategies dashboard.
--
-- Until now `min_cumulative_sharpe` was a single global value stored as
-- pipeline_config['min_cumulative_sharpe'] = 3.0 and read once per cycle
-- by regime_blended_sizer._sharpe_cadence_path. Operator (2026-05-21)
-- asked for FOUR per-regime gates so the conviction floor can be raised
-- in calm regimes (LOW_VOL) and relaxed in dislocated ones (CRISIS).
--
-- Strategy:
--   1. ADD column `min_cumulative_sharpe` REAL NOT NULL DEFAULT 3.0
--      to regime_sizer_params (per-regime row already exists; this
--      slots cleanly alongside liquidity_param, min_signal_notional_pct,
--      position_circuit_breaker_pct — same shape as the existing knobs
--      already surfaced on the dashboard's regime sizing card).
--   2. Bound to [1.0, 10.0] via CHECK constraint matching the slider
--      range. 3.0 is the operator-approved default and matches the
--      legacy pipeline_config value, so day-1 behavior is unchanged.
--   3. Leave the legacy pipeline_config row in place (master-DB invariant
--      forbids DELETE). The sizer prefers the per-regime value and
--      ignores the legacy key going forward.

ALTER TABLE regime_sizer_params
  ADD COLUMN IF NOT EXISTS min_cumulative_sharpe REAL NOT NULL DEFAULT 3.0;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'regime_sizer_params_min_cum_sharpe_check'
  ) THEN
    ALTER TABLE regime_sizer_params
      ADD CONSTRAINT regime_sizer_params_min_cum_sharpe_check
      CHECK (min_cumulative_sharpe >= 1.0 AND min_cumulative_sharpe <= 10.0);
  END IF;
END $$;
