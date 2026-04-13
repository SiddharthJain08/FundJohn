-- Schema v2: datetime precision, DB-backed universe, pipeline config

-- ── 1. price_data: date → datetime ───────────────────────────────────────────
-- Add TIMESTAMPTZ column. Daily data stores as midnight UTC.
-- Sub-daily data (hourly, minute) slots in naturally when API tier allows.

ALTER TABLE price_data ADD COLUMN IF NOT EXISTS datetime TIMESTAMPTZ;

-- Backfill from existing date column
UPDATE price_data SET datetime = date::timestamptz WHERE datetime IS NULL;

ALTER TABLE price_data ALTER COLUMN datetime SET NOT NULL;

-- Swap unique constraint: (ticker, date) → (ticker, datetime)
ALTER TABLE price_data DROP CONSTRAINT IF EXISTS price_data_ticker_date_key;
ALTER TABLE price_data ADD CONSTRAINT price_data_ticker_datetime_key UNIQUE (ticker, datetime);

-- Update indexes
DROP INDEX IF EXISTS idx_price_data_ticker_date;
DROP INDEX IF EXISTS idx_price_data_date;
CREATE INDEX IF NOT EXISTS idx_price_data_ticker_datetime ON price_data(ticker, datetime DESC);
CREATE INDEX IF NOT EXISTS idx_price_data_datetime ON price_data(datetime DESC);

-- Keep date column — still useful for daily-range queries (WHERE date BETWEEN ...)


-- ── 2. universe_config: DB-backed asset universe ──────────────────────────────
-- Replaces the hardcoded SP100 array in universe.js.
-- Add tickers here to expand coverage; set active=false to exclude.

CREATE TABLE IF NOT EXISTS universe_config (
    ticker          TEXT PRIMARY KEY,
    name            TEXT,
    sector          TEXT,
    industry        TEXT,
    index_membership TEXT[] DEFAULT '{}',
    active          BOOLEAN DEFAULT true,
    min_history_days INTEGER DEFAULT 3650,   -- per-ticker history target
    added_at        TIMESTAMPTZ DEFAULT NOW(),
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_universe_config_active ON universe_config(active);
CREATE INDEX IF NOT EXISTS idx_universe_config_index  ON universe_config USING GIN(index_membership);

-- Seed from existing universe table if populated, else from hardcoded list
INSERT INTO universe_config (ticker, name, sector, industry, index_membership, active)
SELECT ticker, name, sector, industry, COALESCE(index_membership, ARRAY['SP100']), active
FROM universe
ON CONFLICT (ticker) DO NOTHING;

-- Seed SP100 tickers that may not be in universe table yet
INSERT INTO universe_config (ticker, index_membership, active) VALUES
('AAPL','{"SP100"}',true),('ABBV','{"SP100"}',true),('ABT','{"SP100"}',true),
('ACN','{"SP100"}',true),('ADBE','{"SP100"}',true),('AIG','{"SP100"}',true),
('AMD','{"SP100"}',true),('AMGN','{"SP100"}',true),('AMZN','{"SP100"}',true),
('AVGO','{"SP100"}',true),('AXP','{"SP100"}',true),('BA','{"SP100"}',true),
('BAC','{"SP100"}',true),('BK','{"SP100"}',true),('BKNG','{"SP100"}',true),
('BLK','{"SP100"}',true),('BMY','{"SP100"}',true),('BRK-B','{"SP100"}',true),
('C','{"SP100"}',true),('CAT','{"SP100"}',true),('CHTR','{"SP100"}',true),
('CL','{"SP100"}',true),('CMCSA','{"SP100"}',true),('COF','{"SP100"}',true),
('COP','{"SP100"}',true),('COST','{"SP100"}',true),('CRM','{"SP100"}',true),
('CSCO','{"SP100"}',true),('CVS','{"SP100"}',true),('CVX','{"SP100"}',true),
('DE','{"SP100"}',true),('DHR','{"SP100"}',true),('DIS','{"SP100"}',true),
('DOW','{"SP100"}',true),('DUK','{"SP100"}',true),('EMR','{"SP100"}',true),
('EOG','{"SP100"}',true),('EXC','{"SP100"}',true),('F','{"SP100"}',true),
('FDX','{"SP100"}',true),('GD','{"SP100"}',true),('GE','{"SP100"}',true),
('GILD','{"SP100"}',true),('GM','{"SP100"}',true),('GOOGL','{"SP100"}',true),
('GS','{"SP100"}',true),('HD','{"SP100"}',true),('HON','{"SP100"}',true),
('IBM','{"SP100"}',true),('INTC','{"SP100"}',true),('INTU','{"SP100"}',true),
('ISRG','{"SP100"}',true),('JNJ','{"SP100"}',true),('JPM','{"SP100"}',true),
('KO','{"SP100"}',true),('LIN','{"SP100"}',true),('LLY','{"SP100"}',true),
('LMT','{"SP100"}',true),('LOW','{"SP100"}',true),('MA','{"SP100"}',true),
('MCD','{"SP100"}',true),('MDLZ','{"SP100"}',true),('MDT','{"SP100"}',true),
('MET','{"SP100"}',true),('META','{"SP100"}',true),('MMM','{"SP100"}',true),
('MO','{"SP100"}',true),('MRK','{"SP100"}',true),('MS','{"SP100"}',true),
('MSFT','{"SP100"}',true),('NEE','{"SP100"}',true),('NFLX','{"SP100"}',true),
('NKE','{"SP100"}',true),('NVDA','{"SP100"}',true),('ORCL','{"SP100"}',true),
('PEP','{"SP100"}',true),('PFE','{"SP100"}',true),('PG','{"SP100"}',true),
('PM','{"SP100"}',true),('PYPL','{"SP100"}',true),('QCOM','{"SP100"}',true),
('RTX','{"SP100"}',true),('SBUX','{"SP100"}',true),('SCHW','{"SP100"}',true),
('SO','{"SP100"}',true),('SPGI','{"SP100"}',true),('SYK','{"SP100"}',true),
('T','{"SP100"}',true),('TGT','{"SP100"}',true),('TMO','{"SP100"}',true),
('TSLA','{"SP100"}',true),('TXN','{"SP100"}',true),('UNH','{"SP100"}',true),
('UPS','{"SP100"}',true),('USB','{"SP100"}',true),('V','{"SP100"}',true),
('VZ','{"SP100"}',true),('WFC','{"SP100"}',true),('WMT','{"SP100"}',true),
('XOM','{"SP100"}',true)
ON CONFLICT (ticker) DO NOTHING;


-- ── 3. pipeline_config: runtime-configurable pipeline settings ────────────────
-- All pipeline constants move here. Update a row = change takes effect next run.

CREATE TABLE IF NOT EXISTS pipeline_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO pipeline_config (key, value, description) VALUES
    ('collection_enabled',   'true',          'Master on/off switch for background collection'),
    ('universe',             'SP100',          'Active universe filter: SP100, SP500, ALL'),
    ('min_interval',         '1d',             'Minimum time interval: 1d | 1h | 5m | 1m'),
    ('history_days',         '3650',           'Target history depth in days'),
    ('polygon_req_per_min',  '5',              'Polygon API requests per minute (free=5, starter=100)'),
    ('fmp_req_per_sec',      '2',              'FMP API requests per second'),
    ('collect_prices',       'true',           'Phase 2: historical OHLCV prices'),
    ('collect_options',      'true',           'Phase 3: options chains + Greeks'),
    ('collect_technicals',   'true',           'Phase 4: RSI, SMA indicators'),
    ('collect_fundamentals', 'true',           'Phase 5: FMP income statements'),
    ('indicators',           'rsi_14,sma_50,sma_200', 'Active indicator set (comma-separated)')
ON CONFLICT (key) DO NOTHING;
