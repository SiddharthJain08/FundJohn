-- 156: per-ticker ownership ledger.
-- Stream B item 15 (docs/specs/2026-09-12-quantdinger-adoptions-spec.md:167-181).
--
-- APPEND-ONLY: one row per (cycle_date, ticker), written by the reconcile step.
-- account_qty is the broker's signed share count; signal_qty is what the open
-- execution_signals rows claim (entry fills netted by attributed exit fills);
-- unknown_qty = account_qty - signal_qty; status is 'ok' | 'unallocated'
-- (broker holds shares no signal claims) | 'shortfall' (signals claim shares
-- the broker does not hold). The sizer reads only the newest cycle_date, and
-- only when OPENCLAW_OWNERSHIP_BLOCK=1.
CREATE TABLE IF NOT EXISTS position_ownership (
  cycle_date   DATE NOT NULL,
  ticker       TEXT NOT NULL,
  account_qty  NUMERIC,
  signal_qty   NUMERIC,
  unknown_qty  NUMERIC,
  status       TEXT,
  created_at   TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (cycle_date, ticker)
);

CREATE INDEX IF NOT EXISTS position_ownership_ticker_idx
  ON position_ownership (ticker, cycle_date DESC);
CREATE INDEX IF NOT EXISTS position_ownership_status_idx
  ON position_ownership (cycle_date DESC, status);
