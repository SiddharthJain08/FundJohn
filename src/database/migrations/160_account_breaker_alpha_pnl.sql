-- 160: account breaker — drawdown on CUMULATIVE ALPHA P&L (spec C1 Amendment 2,
-- docs/specs/2026-09-12-quantdinger-adoptions-spec.md, operator-ruled 2026-09-28).
-- ADDITIVE ONLY: one new table + two nullable columns. Nothing dropped/deleted.
--
-- account_breaker_alpha_epoch: snapshot of the open NON-benchmark lots taken ONCE,
-- on the first breaker tick under the new rule (same transaction that sets
-- account_breaker_state.alpha_epoch_at). It seeds the FIFO realized-P&L leg over
-- broker_fills (155). Never rewritten; operator re-arm never moves the epoch.
CREATE TABLE IF NOT EXISTS account_breaker_alpha_epoch (
  ticker           TEXT NOT NULL,
  qty              NUMERIC NOT NULL,      -- magnitude (positive)
  avg_entry_price  NUMERIC,
  side             TEXT,                  -- 'long' | 'short'
  taken_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS account_breaker_alpha_epoch_ticker_idx
  ON account_breaker_alpha_epoch (ticker);

ALTER TABLE account_breaker_state
  ADD COLUMN IF NOT EXISTS peak_alpha_pnl NUMERIC,
  ADD COLUMN IF NOT EXISTS alpha_epoch_at TIMESTAMPTZ;

COMMENT ON COLUMN account_breaker_state.peak_alpha_pnl IS
  'high-water mark of cumulative alpha P&L ($); reset to the current alpha_pnl on operator re-arm (C1 amendment 2)';
COMMENT ON COLUMN account_breaker_state.alpha_epoch_at IS
  'first tick under the alpha-P&L rule; NULL = epoch not yet taken. Never moved once set.';
