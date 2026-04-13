-- Master coverage registry
-- Tracks exactly what date ranges are stored per ticker per data type.
-- The pipeline checks this BEFORE making any API call — never re-fetches known data.

CREATE TABLE IF NOT EXISTS data_coverage (
    ticker      TEXT NOT NULL,
    data_type   TEXT NOT NULL,   -- 'prices', 'options', 'technicals', 'fundamentals'
    date_from   DATE NOT NULL,   -- earliest date stored
    date_to     DATE NOT NULL,   -- latest date stored
    rows_stored INTEGER DEFAULT 0,
    last_updated TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ticker, data_type)
);

CREATE INDEX IF NOT EXISTS idx_coverage_type ON data_coverage(data_type, date_from, date_to);

-- Backfill coverage from existing price_data so we don't re-fetch anything already stored
INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
SELECT
    ticker,
    'prices',
    MIN(date),
    MAX(date),
    COUNT(*),
    NOW()
FROM price_data
GROUP BY ticker
ON CONFLICT (ticker, data_type) DO UPDATE SET
    date_from    = LEAST(EXCLUDED.date_from, data_coverage.date_from),
    date_to      = GREATEST(EXCLUDED.date_to, data_coverage.date_to),
    rows_stored  = EXCLUDED.rows_stored,
    last_updated = NOW();

-- Backfill for options, technicals, fundamentals
INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
SELECT ticker, 'options', MIN(snapshot_date), MAX(snapshot_date), COUNT(*), NOW()
FROM options_data GROUP BY ticker
ON CONFLICT (ticker, data_type) DO UPDATE SET
    date_from = LEAST(EXCLUDED.date_from, data_coverage.date_from),
    date_to   = GREATEST(EXCLUDED.date_to, data_coverage.date_to),
    rows_stored = EXCLUDED.rows_stored, last_updated = NOW();

INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
SELECT ticker, 'technicals', MIN(date), MAX(date), COUNT(*), NOW()
FROM technicals GROUP BY ticker
ON CONFLICT (ticker, data_type) DO UPDATE SET
    date_from = LEAST(EXCLUDED.date_from, data_coverage.date_from),
    date_to   = GREATEST(EXCLUDED.date_to, data_coverage.date_to),
    rows_stored = EXCLUDED.rows_stored, last_updated = NOW();

INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
SELECT ticker, 'fundamentals', MIN(period_end), MAX(period_end), COUNT(*), NOW()
FROM fundamentals GROUP BY ticker
ON CONFLICT (ticker, data_type) DO UPDATE SET
    date_from = LEAST(EXCLUDED.date_from, data_coverage.date_from),
    date_to   = GREATEST(EXCLUDED.date_to, data_coverage.date_to),
    rows_stored = EXCLUDED.rows_stored, last_updated = NOW();
