-- OUE (Over / Under / Expected) per-closed-trade classification.
--
-- Operator (2026-05-16) replaced the OLD `OUR` metric (Over / Under /
-- Rejected-signals, summed across daily cycles) with `OUE`: each closed
-- trade is classified once based on realized PnL vs the GBM-based
-- expectation captured at signal time. The invariant is now O + U + E =
-- # closed trades for that strategy (lifetime).
--
-- Classification rule (matches existing SIGMA_GATE used by
-- performance_outliers, but applied ONLY to status='closed' rows with a
-- non-null realized_pnl_pct):
--   sigma_delta = (realized_pnl_pct - ev_gbm) / sigma_holding
--     where sigma_holding = hv21 × sqrt(days_held / 252)
--   |sigma_delta| ≥ SIGMA_GATE → 'over' or 'under'
--   otherwise                → 'expected'
--
-- ev_gbm + hv21 live in the structured-handoff JSON at signal time, not
-- in the master DB — so the OUE classifier reads those at close time and
-- persists the result here. Pre-cutover closes get a one-shot backfill.

ALTER TABLE execution_signals
  ADD COLUMN IF NOT EXISTS oue_kind         TEXT,
  ADD COLUMN IF NOT EXISTS oue_sigma_delta  NUMERIC,
  ADD COLUMN IF NOT EXISTS oue_classified_at TIMESTAMPTZ;

ALTER TABLE execution_signals
  ADD CONSTRAINT execution_signals_oue_kind_check
    CHECK (oue_kind IN ('over', 'under', 'expected') OR oue_kind IS NULL)
  NOT VALID;

ALTER TABLE execution_signals
  VALIDATE CONSTRAINT execution_signals_oue_kind_check;

CREATE INDEX IF NOT EXISTS idx_execution_signals_oue
  ON execution_signals (strategy_id, oue_kind)
  WHERE oue_kind IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_execution_signals_oue_classified_at
  ON execution_signals (oue_classified_at DESC)
  WHERE oue_classified_at IS NOT NULL;
