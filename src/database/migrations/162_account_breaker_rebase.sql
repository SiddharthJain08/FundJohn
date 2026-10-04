-- 162: account breaker — per-ticker REBASE of the alpha-P&L ledger (Breaker alpha
-- P&L, Task 4; spec C1 Amendment 2b, docs/specs/2026-09-12-quantdinger-adoptions-spec.md,
-- operator-ruled 2026-10-04). ADDITIVE ONLY: four new columns on one existing table
-- and one new table. Rows are only ever appended or marked, never removed.
--
-- account_breaker_alpha_epoch keeps ONE 'epoch' row per ticker (migration 160) and
-- gains any number of later 'rebase' rows. A rebase row re-seeds ONE ticker's
-- average-cost book from the BROKER position at taken_at (qty 0 / NULL avg when the
-- broker holds none) and carries that ticker's realized P&L so far in realized_carry,
-- so realized_carry + the broker's own unrealized P&L is continuous across it.
-- It never moves account_breaker_state.alpha_epoch_at and never touches the
-- high-water mark peak_alpha_pnl.
ALTER TABLE account_breaker_alpha_epoch
  ADD COLUMN IF NOT EXISTS kind           TEXT    NOT NULL DEFAULT 'epoch',   -- 'epoch' | 'rebase'
  ADD COLUMN IF NOT EXISTS realized_carry NUMERIC NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS reason         TEXT,                               -- 'auto:<TYPE>' | 'operator:<text>'
  ADD COLUMN IF NOT EXISTS activity_ref   TEXT;                               -- broker activity id that explained it

-- account_breaker_recon_watch: continuity of an unreconciled ticker, so the breaker
-- can escalate to the operator after REBASE_ESCALATE_S of continuous distrust.
-- Upserted while a ticker mismatches; cleared_at is SET (never deleted) when it
-- reconciles; a later mismatch starts a new episode on the same row.
CREATE TABLE IF NOT EXISTS account_breaker_recon_watch (
  ticker        TEXT PRIMARY KEY,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at  TIMESTAMPTZ NOT NULL,
  notified_at   TIMESTAMPTZ,
  cleared_at    TIMESTAMPTZ,
  ledger_qty    NUMERIC,                  -- the (ledger, broker) quantity pair last seen;
  broker_qty    NUMERIC                   -- a changed pair restarts the episode (lagged fill)
);
