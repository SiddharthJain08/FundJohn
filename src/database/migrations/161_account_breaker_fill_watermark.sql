-- 161: account breaker — persisted FILL WATERMARK (Breaker alpha P&L, Task 2 / P1,
-- operator-ruled after the 2026-09-30 re-review).
-- ADDITIVE ONLY: one nullable column. Nothing dropped/deleted/rewritten.
--
-- last_synced_filled_at: the max broker `filled_at` among the FILL activities the
-- breaker has already upserted into broker_fills (155). The next tick narrows its
-- BROKER fetch to max(last_synced_filled_at - 10 min, alpha_epoch_at); the alpha
-- ledger is still computed from the FULL since-epoch set read from broker_fills in
-- SQL, so the watermark only bounds the broker call. Advanced only after a
-- successful upsert; NULL = never synced (fetch from the epoch).
ALTER TABLE account_breaker_state
  ADD COLUMN IF NOT EXISTS last_synced_filled_at TIMESTAMPTZ;

COMMENT ON COLUMN account_breaker_state.last_synced_filled_at IS
  'max filled_at of the fills the breaker has ingested into broker_fills; the next fetch starts 10 min earlier (overlap; ON CONFLICT DO NOTHING absorbs it). NULL = fetch from alpha_epoch_at.';
