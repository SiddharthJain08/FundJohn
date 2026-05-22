-- 111_ticker_metadata_snapshots.sql
--
-- Point-in-time TickerMetadata source. Daily writes from
-- ticker_metadata_writer.py; monthly historical from Phase B backfill.
-- Resolver always reads the latest row where snapshot_date <= as_of.
-- Append-only per master invariant; bad rows go through data_quarantine.

CREATE TABLE IF NOT EXISTS ticker_metadata_snapshots (
  snapshot_date     DATE NOT NULL,
  symbol            TEXT NOT NULL,
  asset_class       TEXT NOT NULL,
  exchange          TEXT,
  status            TEXT NOT NULL,
  tradable          BOOLEAN NOT NULL DEFAULT FALSE,
  shortable         BOOLEAN NOT NULL DEFAULT FALSE,
  fractionable      BOOLEAN NOT NULL DEFAULT FALSE,
  easy_to_borrow    BOOLEAN NOT NULL DEFAULT FALSE,
  market_cap        NUMERIC,
  adv_usd_20d       NUMERIC,
  sector            TEXT,
  industry          TEXT,
  options_eligible  BOOLEAN NOT NULL DEFAULT FALSE,
  in_sp500          BOOLEAN NOT NULL DEFAULT FALSE,
  in_r1000          BOOLEAN NOT NULL DEFAULT FALSE,
  in_r3000          BOOLEAN NOT NULL DEFAULT FALSE,
  listed_date       DATE,
  delisted_date     DATE,
  source_tag        TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (snapshot_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_meta_snapshots_symbol_date
  ON ticker_metadata_snapshots(symbol, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_meta_snapshots_date_active
  ON ticker_metadata_snapshots(snapshot_date)
  WHERE status='active' AND tradable=TRUE;
