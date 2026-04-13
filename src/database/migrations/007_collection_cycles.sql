-- Track full collection cycle metrics per run

CREATE TABLE IF NOT EXISTS collection_cycles (
  id              BIGSERIAL PRIMARY KEY,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at    TIMESTAMPTZ,
  duration_ms     INTEGER,
  -- Per-phase row counts
  snapshot_tickers    INTEGER DEFAULT 0,
  price_rows          INTEGER DEFAULT 0,
  options_contracts   INTEGER DEFAULT 0,
  technical_rows      INTEGER DEFAULT 0,
  fundamental_records INTEGER DEFAULT 0,
  -- API calls per source
  polygon_calls   INTEGER DEFAULT 0,
  fmp_calls       INTEGER DEFAULT 0,
  yfinance_calls  INTEGER DEFAULT 0,
  -- Totals
  total_rows      INTEGER DEFAULT 0,
  errors          INTEGER DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'running'  -- running | complete | failed
);

CREATE INDEX IF NOT EXISTS idx_collection_cycles_started ON collection_cycles (started_at DESC);
