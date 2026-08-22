-- 147: per-regime ACTING-STRATEGY conviction gate (operator directive
-- 2026-08-22). The sizer's ticker-selection gate is now the minimum number of
-- DISTINCT strategies acting on a ticker in its net direction, replacing the
-- corr-adjusted cumulative-Sharpe floor (min_corr_cum_sharpe, migration 140).
-- That column is retained (append-only DB; all four regimes sat at 0.0 = gate
-- open) but is no longer read or written. Bound [1, 10]: 1 = every ticker with
-- a contributor passes (the pre-2026-08-22 book, byte-identical); 10 = only
-- tickers ten strategies agree on. Default 1 so the rollout changes nothing
-- until the operator moves a dashboard slider. Idempotent.
ALTER TABLE regime_sizer_params
  ADD COLUMN IF NOT EXISTS min_acting_strategies INTEGER NOT NULL DEFAULT 1;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'regime_sizer_params_min_acting_strategies_check'
  ) THEN
    ALTER TABLE regime_sizer_params
      ADD CONSTRAINT regime_sizer_params_min_acting_strategies_check
      CHECK (min_acting_strategies >= 1 AND min_acting_strategies <= 10);
  END IF;
END $$;

COMMENT ON COLUMN regime_sizer_params.min_acting_strategies IS
  'Conviction gate (2026-08-22): minimum number of distinct strategies acting on a ticker in its net direction for the sizer to take the position. [1,10]; 1 = open.';
COMMENT ON COLUMN regime_sizer_params.min_corr_cum_sharpe IS
  'RETIRED 2026-08-22 (replaced by min_acting_strategies). Unread; retained append-only.';
