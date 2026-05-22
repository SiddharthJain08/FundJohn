-- 114_data_quarantine.sql
-- Master-parquet append-only invariant means bad rows cannot be DELETEd.
-- Instead, flag them here so all consumers (collector, backtest engine,
-- universe_resolver coverage_floor, dashboard) skip them at read time.

CREATE TABLE IF NOT EXISTS data_quarantine (
  id                BIGSERIAL PRIMARY KEY,
  master_table      TEXT NOT NULL,                  -- 'prices.parquet', 'options_eod.parquet', 'ticker_metadata_snapshots', ...
  symbol            TEXT NOT NULL,
  affected_date     DATE NOT NULL,
  source_tag        TEXT NOT NULL,                  -- which backfill version produced the bad row
  reason            TEXT NOT NULL,
  flagged_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  flagged_by        TEXT NOT NULL,                  -- operator email or 'auto:<check-name>'
  superseded_by_source_tag TEXT,                    -- set when a corrected backfill writes a new row
  superseded_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_quarantine_lookup ON data_quarantine(master_table, symbol, affected_date) WHERE superseded_at IS NULL;
