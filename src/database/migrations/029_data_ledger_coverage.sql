-- Add parquet coverage metadata to data_columns so sync_data_ledger.py can store
-- date range and size info, and ResearchJohn can gate on data depth.
ALTER TABLE data_columns
  ADD COLUMN IF NOT EXISTS min_date     DATE,
  ADD COLUMN IF NOT EXISTS max_date     DATE,
  ADD COLUMN IF NOT EXISTS row_count    BIGINT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS ticker_count INT    DEFAULT 0;
