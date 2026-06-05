-- 127_sp6_b0_fill_persistence.sql
-- SP-6 Phase B0: per-order execution ledger.
--
-- Additive only (master-DB NEVER-DELETE invariant): two nullable columns, NO DEFAULT,
-- on alpaca_submissions (the order grain — one row per consolidated broker order; the
-- real fill already lives here as filled_avg_price/filled_qty via alpaca_reconcile).
--   official_close  : official close[T+1] for this order's ticker (beat-close benchmark)
--   exec_ledger_usd : (official_close - filled_avg_price) x (direction_sign x filled_qty)
ALTER TABLE alpaca_submissions
    ADD COLUMN IF NOT EXISTS official_close  NUMERIC,
    ADD COLUMN IF NOT EXISTS exec_ledger_usd NUMERIC;
