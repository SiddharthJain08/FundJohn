-- 153: account epoch (2026-09-04 paper-account cutover to PA3K16GEOQ4E).
-- The DB ledgers are append-only across broker-account cutovers; the
-- portfolio page (closed trades, win rate, avg/best/worst, pnl curve)
-- counts only rows at/after this date. Bump the value on the next cutover.
-- Idempotent: never overwrites an operator-edited value.
INSERT INTO pipeline_config (key, value, description, updated_at)
-- 2026-09-05, not -04: the cutover happened the evening of 09-04, so every
-- close stamped 09-04 (including the old book's final-day closes and the
-- stale-tracker sweep) belongs to the OLD account.
VALUES ('account_epoch', '2026-09-05',
        'Date (YYYY-MM-DD) of the most recent broker-account cutover. Portfolio-page stats (closed trades, win rate, realized P&L, pnl curve) count only signals whose signal_date is at/after this date, so a fresh account starts from zero without deleting ledger history and late closes of old-account leftovers never leak in.',
        NOW())
ON CONFLICT (key) DO NOTHING;
