-- SP-7 Phase A2: true listing dates for point-in-time universe membership.
-- first_seen_at is refresh-log-derived (~2026-05-14 for everything) and unusable
-- for historical PIT filters. listed_date = earliest Alpaca daily bar.
ALTER TABLE alpaca_tradable_universe
    ADD COLUMN IF NOT EXISTS listed_date DATE;
