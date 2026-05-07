-- 066_alpaca_liquidations.sql
--
-- Audit table for regime-change liquidations written by
-- src/execution/regime_liquidator.py. The 9:00 AM ET cron compares
-- regime_latest.json's `prior_state` vs `state`; on any change it
-- cancels all open OpenClaw orders (COID prefix 'AX') and submits a
-- market close per OpenClaw symbol still showing a position. One row
-- per (regime_event, symbol) lands here.
--
-- Append-only, per the CLAUDE.md core invariant. No DELETE or DROP
-- paths exist; the operator inspects history through this table and
-- the doctor's check_liquidator_audit cross-references it against
-- market_regime to flag silent failures.

CREATE TABLE IF NOT EXISTS alpaca_liquidations (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  triggered_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  run_date                DATE NOT NULL,
  regime_from             TEXT NOT NULL,
  regime_to               TEXT NOT NULL,
  symbol                  TEXT NOT NULL,
  qty                     NUMERIC,
  side_closed             TEXT,                      -- 'long_close' | 'short_close'
  parent_client_order_ids TEXT[],                    -- OpenClaw COIDs sourcing this symbol
  cancel_results          JSONB,                     -- { order_id: {ok, role, detail}, ... }
  close_result            JSONB,                     -- broker response for the close order
  result_status           TEXT NOT NULL,             -- 'closed' | 'cancel_only' | 'error' | 'dry_run'
  error_msg               TEXT
);

CREATE INDEX IF NOT EXISTS alpaca_liquidations_run_idx
  ON alpaca_liquidations (run_date DESC, regime_from, regime_to);
