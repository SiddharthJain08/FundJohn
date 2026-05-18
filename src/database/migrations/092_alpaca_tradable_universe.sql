-- 092_alpaca_tradable_universe.sql
--
-- Persistent snapshot of Alpaca's master asset list, refreshed daily by
-- src/maintenance/refresh_tradable_universe.py. trade_handoff_builder
-- joins against this to filter out signals on delisted/halted/untradable
-- tickers BEFORE they enter the sized handoff (rather than burning an
-- Alpaca submission and eating a 422 "asset is not active" rejection).
--
-- Append-only per the never-delete-master-data invariant: tickers that
-- drop out of Alpaca's list keep their row but get status='inactive' +
-- tradable=false; last_seen_at stops advancing so we can audit when a
-- ticker fell off. Re-listings flip the flag back without losing
-- first_seen_at history.

CREATE TABLE IF NOT EXISTS alpaca_tradable_universe (
  symbol          TEXT PRIMARY KEY,
  alpaca_asset_id TEXT,
  name            TEXT,
  asset_class     TEXT,
  exchange        TEXT,
  status          TEXT NOT NULL,    -- 'active' / 'inactive' (we mark inactive on drop-from-list)
  tradable        BOOLEAN NOT NULL DEFAULT FALSE,
  shortable       BOOLEAN NOT NULL DEFAULT FALSE,
  marginable      BOOLEAN NOT NULL DEFAULT FALSE,
  fractionable    BOOLEAN NOT NULL DEFAULT FALSE,
  easy_to_borrow  BOOLEAN NOT NULL DEFAULT FALSE,
  first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alpaca_universe_tradable
  ON alpaca_tradable_universe (tradable, status)
  WHERE tradable = TRUE AND status = 'active';

CREATE INDEX IF NOT EXISTS idx_alpaca_universe_last_seen
  ON alpaca_tradable_universe (last_seen_at DESC);

-- Refresh run audit so doctor / system_checks can read freshness and
-- the daily maintenance digest can report add/drop counts.
CREATE TABLE IF NOT EXISTS alpaca_tradable_universe_refresh_log (
  id                BIGSERIAL PRIMARY KEY,
  run_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  total_active      INT NOT NULL,
  total_tradable    INT NOT NULL,
  newly_listed      INT NOT NULL DEFAULT 0,
  newly_inactive    INT NOT NULL DEFAULT 0,
  status_changes    INT NOT NULL DEFAULT 0,
  alpaca_api_ms     INT,
  notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_alpaca_universe_refresh_log_run_at
  ON alpaca_tradable_universe_refresh_log (run_at DESC);
