-- Per-regime floor for the correlation-adjusted cumulative-Sharpe conviction
-- gate (OPENCLAW_STRATEGY_CORR_CUMSHARPE). Distinct from the legacy
-- min_cumulative_sharpe [1.0,10.0] floor (migration 108): the corr-adjusted
-- quantity is cadence-normalized AND diversification-deflated, so its scale is
-- smaller and is set empirically from the shadow run. Bound [0.0, 10.0] so the
-- operator can fully open the gate in a regime (e.g. CRISIS). Additive column;
-- master-DB invariant honored (no DELETE). Default 1.0 is a PLACEHOLDER — the
-- rollout REQUIRES setting per-regime values from shadow data before the live flip.
ALTER TABLE regime_sizer_params
  ADD COLUMN IF NOT EXISTS min_corr_cum_sharpe REAL NOT NULL DEFAULT 1.0;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'regime_sizer_params_min_corr_cum_sharpe_check'
  ) THEN
    ALTER TABLE regime_sizer_params
      ADD CONSTRAINT regime_sizer_params_min_corr_cum_sharpe_check
      CHECK (min_corr_cum_sharpe >= 0.0 AND min_corr_cum_sharpe <= 10.0);
  END IF;
END $$;
