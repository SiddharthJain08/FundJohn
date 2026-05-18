-- Phase: regime sizer migration to a NAV-fraction representation.
--
-- Until now `min_signal_notional_usd` stored an absolute dollar floor.
-- That works only while NAV stays near $100k; if the account grows to
-- $250k the same $100 floor is meaningless. Operator (2026-05-16)
-- requested a percentage-of-portfolio representation so the floor
-- tracks NAV automatically and the dashboard surface matches the
-- other regime params (liquidity_param, position_circuit_breaker_pct).
--
-- Strategy:
--   1. ADD column `min_signal_notional_pct` (decimal fraction of NAV,
--      e.g. 0.001 = 0.1% of NAV). NOT NULL after backfill.
--   2. Backfill from the existing USD value assuming a baseline NAV of
--      $100,000 (the operator's reference equity). This preserves
--      observed behavior on day 1; the operator can re-tune via the
--      dashboard from there.
--   3. Leave the legacy `min_signal_notional_usd` column in place
--      (master-DB invariant: never DROP). Downstream code will prefer
--      the new column when present, falling back to USD otherwise.

ALTER TABLE regime_sizer_params
  ADD COLUMN IF NOT EXISTS min_signal_notional_pct NUMERIC;

UPDATE regime_sizer_params
   SET min_signal_notional_pct = min_signal_notional_usd / 100000.0
 WHERE min_signal_notional_pct IS NULL;

ALTER TABLE regime_sizer_params
  ALTER COLUMN min_signal_notional_pct SET NOT NULL,
  ADD CONSTRAINT min_signal_notional_pct_range
    CHECK (min_signal_notional_pct >= 0 AND min_signal_notional_pct <= 0.5);
