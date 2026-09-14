-- 155: broker-fill fact table + fill timestamps + exit-leg slippage.
-- Stream B item 14 (docs/specs/2026-09-12-quantdinger-adoptions-spec.md:144-166).
--
-- broker_fills is APPEND-ONLY: one row per Alpaca FILL activity, keyed by the
-- activity id the broker assigns. alpaca_reconcile ingests with
-- ON CONFLICT DO NOTHING, so re-running the reconcile step (or the --sweep-stale
-- pass) never duplicates and never rewrites a row.
--
-- The four order-shape columns (parent_order_id, client_order_id, order_type,
-- order_class) are NOT carried by Alpaca activity records, and the REST order
-- model has no parent pointer at all — a leg's parent is only knowable by
-- walking `legs` on a --nested order read. alpaca_reconcile enriches from that
-- read and leaves these NULL when the order fell outside the window.
CREATE TABLE IF NOT EXISTS broker_fills (
  activity_id      TEXT PRIMARY KEY,
  order_id         TEXT,
  parent_order_id  TEXT,
  client_order_id  TEXT,
  ticker           TEXT,
  side             TEXT,
  order_type       TEXT,
  order_class      TEXT,
  qty              NUMERIC,
  price            NUMERIC,
  filled_at        TIMESTAMPTZ,
  ingested_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS broker_fills_filled_at_idx
  ON broker_fills (filled_at DESC);
CREATE INDEX IF NOT EXISTS broker_fills_order_idx
  ON broker_fills (order_id);
CREATE INDEX IF NOT EXISTS broker_fills_parent_idx
  ON broker_fills (parent_order_id) WHERE parent_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS broker_fills_ticker_idx
  ON broker_fills (ticker, filled_at DESC);

-- Broker fill timestamp on the submission ledger. submitted_at already exists
-- (043_alpaca_submissions.sql:25) and reconciled_at (064) records when WE
-- looked, not when the broker filled. latency = filled_at - submitted_at.
ALTER TABLE alpaca_submissions ADD COLUMN IF NOT EXISTS filled_at TIMESTAMPTZ;

-- Exit-leg realized slippage vs the signal's own stop/target level, signed
-- adverse-positive. Nullable: only exit legs we can attribute get a value.
-- execution_signals.fill_slippage_bps (migration 145) is the ENTRY twin.
ALTER TABLE signal_pnl ADD COLUMN IF NOT EXISTS exit_slippage_bps NUMERIC;
