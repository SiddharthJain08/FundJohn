-- 064_alpaca_submissions_reconciliation.sql
--
-- Daily reconciliation against Alpaca FILL activities (Phase 1.3 of
-- alpaca-cli integration). Adds the columns the new
-- src/execution/alpaca_reconcile.py step writes after the daily `alpaca`
-- pipeline step. Closes the attribution hole where the engine's
-- "would have hit target" arithmetic on parquet prices was credited
-- even when the broker rejected or partially filled the order.
--
-- broker_status values:
--   'filled'              — every share filled
--   'partial'             — some shares filled, leaves_qty > 0
--   'rejected_by_broker'  — submission row exists, alpaca_order_id IS NOT NULL,
--                           but no FILL activity was returned for the order_id
--   NULL                  — not yet reconciled (typical between submit and the
--                           next reconcile pass)

ALTER TABLE alpaca_submissions
  ADD COLUMN IF NOT EXISTS broker_status     TEXT,
  ADD COLUMN IF NOT EXISTS filled_qty        NUMERIC,
  ADD COLUMN IF NOT EXISTS filled_avg_price  NUMERIC,
  ADD COLUMN IF NOT EXISTS reconciled_at     TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS alpaca_submissions_unreconciled_idx
  ON alpaca_submissions (run_date)
  WHERE alpaca_order_id IS NOT NULL AND reconciled_at IS NULL;
