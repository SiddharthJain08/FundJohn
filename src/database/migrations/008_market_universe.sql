-- Migration 008: Expand universe to full market coverage
-- Adds indices, ETFs, crypto, commodities futures, forex
-- New columns on universe_config track instrument behaviour per-asset

-- ── Column additions ──────────────────────────────────────────────────────────
ALTER TABLE universe_config ADD COLUMN IF NOT EXISTS category         TEXT    DEFAULT 'equity';
ALTER TABLE universe_config ADD COLUMN IF NOT EXISTS has_options      BOOLEAN DEFAULT false;
ALTER TABLE universe_config ADD COLUMN IF NOT EXISTS has_fundamentals BOOLEAN DEFAULT false;
ALTER TABLE universe_config ADD COLUMN IF NOT EXISTS snapshot_24h     BOOLEAN DEFAULT false;

-- ── Tag existing SP100 equities ───────────────────────────────────────────────
UPDATE universe_config
SET category='equity', has_options=true, has_fundamentals=true, snapshot_24h=false
WHERE 'SP100' = ANY(index_membership);

-- ── Indices (13) ──────────────────────────────────────────────────────────────
INSERT INTO universe_config (ticker, name, category, index_membership, active, has_options, has_fundamentals, snapshot_24h) VALUES
('^GSPC',    'S&P 500',               'index', ARRAY['INDICES'],  true, false, false, false),
('^DJI',     'Dow Jones Industrials', 'index', ARRAY['INDICES'],  true, false, false, false),
('^IXIC',    'NASDAQ Composite',      'index', ARRAY['INDICES'],  true, false, false, false),
('^RUT',     'Russell 2000',          'index', ARRAY['INDICES'],  true, false, false, false),
('^VIX',     'CBOE Volatility',       'index', ARRAY['INDICES'],  true, false, false, false),
('^TNX',     '10Y Treasury Yield',    'index', ARRAY['INDICES'],  true, false, false, false),
('^TYX',     '30Y Treasury Yield',    'index', ARRAY['INDICES'],  true, false, false, false),
('^FVX',     '5Y Treasury Yield',     'index', ARRAY['INDICES'],  true, false, false, false),
('^STOXX50E','Euro Stoxx 50',         'index', ARRAY['INDICES'],  true, false, false, false),
('^N225',    'Nikkei 225',            'index', ARRAY['INDICES'],  true, false, false, false),
('^HSI',     'Hang Seng',             'index', ARRAY['INDICES'],  true, false, false, false),
('^FTSE',    'FTSE 100',              'index', ARRAY['INDICES'],  true, false, false, false),
('^GDAXI',   'DAX',                   'index', ARRAY['INDICES'],  true, false, false, false)
ON CONFLICT (ticker) DO UPDATE SET
  name=EXCLUDED.name, category=EXCLUDED.category,
  index_membership=EXCLUDED.index_membership, active=EXCLUDED.active,
  has_options=EXCLUDED.has_options, has_fundamentals=EXCLUDED.has_fundamentals,
  snapshot_24h=EXCLUDED.snapshot_24h;

-- ── ETFs — broad market (options: yes) ───────────────────────────────────────
INSERT INTO universe_config (ticker, name, category, index_membership, active, has_options, has_fundamentals, snapshot_24h) VALUES
('SPY',  'SPDR S&P 500 ETF',        'etf', ARRAY['ETFS'], true, true,  false, false),
('QQQ',  'Invesco NASDAQ 100 ETF',  'etf', ARRAY['ETFS'], true, true,  false, false),
('IWM',  'iShares Russell 2000 ETF','etf', ARRAY['ETFS'], true, true,  false, false),
('DIA',  'SPDR Dow Jones ETF',      'etf', ARRAY['ETFS'], true, true,  false, false),
('VTI',  'Vanguard Total Market',   'etf', ARRAY['ETFS'], true, false, false, false),
('VEA',  'Vanguard Developed Mkts', 'etf', ARRAY['ETFS'], true, false, false, false),
('VWO',  'Vanguard Emerging Mkts',  'etf', ARRAY['ETFS'], true, false, false, false)
ON CONFLICT (ticker) DO UPDATE SET
  name=EXCLUDED.name, category=EXCLUDED.category,
  index_membership=EXCLUDED.index_membership, active=EXCLUDED.active,
  has_options=EXCLUDED.has_options, has_fundamentals=EXCLUDED.has_fundamentals,
  snapshot_24h=EXCLUDED.snapshot_24h;

-- ── ETFs — fixed income (options: yes for liquid ones) ────────────────────────
INSERT INTO universe_config (ticker, name, category, index_membership, active, has_options, has_fundamentals, snapshot_24h) VALUES
('TLT',  'iShares 20Y Treasury ETF', 'etf', ARRAY['ETFS'], true, true,  false, false),
('IEF',  'iShares 7-10Y Treasury',   'etf', ARRAY['ETFS'], true, true,  false, false),
('SHY',  'iShares 1-3Y Treasury',    'etf', ARRAY['ETFS'], true, false, false, false),
('HYG',  'iShares High Yield Bond',  'etf', ARRAY['ETFS'], true, true,  false, false),
('LQD',  'iShares IG Corp Bond',     'etf', ARRAY['ETFS'], true, true,  false, false),
('AGG',  'iShares Core US Agg Bond', 'etf', ARRAY['ETFS'], true, false, false, false)
ON CONFLICT (ticker) DO UPDATE SET
  name=EXCLUDED.name, category=EXCLUDED.category,
  index_membership=EXCLUDED.index_membership, active=EXCLUDED.active,
  has_options=EXCLUDED.has_options, has_fundamentals=EXCLUDED.has_fundamentals,
  snapshot_24h=EXCLUDED.snapshot_24h;

-- ── ETFs — commodities (options: yes for GLD/SLV/USO) ────────────────────────
INSERT INTO universe_config (ticker, name, category, index_membership, active, has_options, has_fundamentals, snapshot_24h) VALUES
('GLD',  'SPDR Gold Shares',       'etf', ARRAY['ETFS'], true, true,  false, false),
('SLV',  'iShares Silver Trust',   'etf', ARRAY['ETFS'], true, true,  false, false),
('USO',  'US Oil Fund',            'etf', ARRAY['ETFS'], true, true,  false, false),
('UNG',  'US Natural Gas Fund',    'etf', ARRAY['ETFS'], true, false, false, false),
('PDBC', 'Invesco Commodity ETF',  'etf', ARRAY['ETFS'], true, false, false, false)
ON CONFLICT (ticker) DO UPDATE SET
  name=EXCLUDED.name, category=EXCLUDED.category,
  index_membership=EXCLUDED.index_membership, active=EXCLUDED.active,
  has_options=EXCLUDED.has_options, has_fundamentals=EXCLUDED.has_fundamentals,
  snapshot_24h=EXCLUDED.snapshot_24h;

-- ── ETFs — SPDR sectors (all have liquid options) ─────────────────────────────
INSERT INTO universe_config (ticker, name, category, index_membership, active, has_options, has_fundamentals, snapshot_24h) VALUES
('XLF',  'Financial Select Sector',    'etf', ARRAY['ETFS'], true, true, false, false),
('XLK',  'Technology Select Sector',   'etf', ARRAY['ETFS'], true, true, false, false),
('XLE',  'Energy Select Sector',       'etf', ARRAY['ETFS'], true, true, false, false),
('XLV',  'Health Care Select Sector',  'etf', ARRAY['ETFS'], true, true, false, false),
('XLI',  'Industrial Select Sector',   'etf', ARRAY['ETFS'], true, true, false, false),
('XLY',  'Consumer Discret. Sector',   'etf', ARRAY['ETFS'], true, true, false, false),
('XLP',  'Consumer Staples Sector',    'etf', ARRAY['ETFS'], true, true, false, false),
('XLU',  'Utilities Select Sector',    'etf', ARRAY['ETFS'], true, true, false, false),
('XLRE', 'Real Estate Select Sector',  'etf', ARRAY['ETFS'], true, true, false, false),
('XLB',  'Materials Select Sector',    'etf', ARRAY['ETFS'], true, true, false, false),
('XLC',  'Comm. Services Sector',      'etf', ARRAY['ETFS'], true, true, false, false)
ON CONFLICT (ticker) DO UPDATE SET
  name=EXCLUDED.name, category=EXCLUDED.category,
  index_membership=EXCLUDED.index_membership, active=EXCLUDED.active,
  has_options=EXCLUDED.has_options, has_fundamentals=EXCLUDED.has_fundamentals,
  snapshot_24h=EXCLUDED.snapshot_24h;

-- ── ETFs — international ──────────────────────────────────────────────────────
INSERT INTO universe_config (ticker, name, category, index_membership, active, has_options, has_fundamentals, snapshot_24h) VALUES
('EFA',  'iShares MSCI EAFE',       'etf', ARRAY['ETFS'], true, true,  false, false),
('EEM',  'iShares MSCI Emerging',   'etf', ARRAY['ETFS'], true, true,  false, false),
('EWJ',  'iShares MSCI Japan',      'etf', ARRAY['ETFS'], true, false, false, false),
('FXI',  'iShares China Large Cap', 'etf', ARRAY['ETFS'], true, false, false, false)
ON CONFLICT (ticker) DO UPDATE SET
  name=EXCLUDED.name, category=EXCLUDED.category,
  index_membership=EXCLUDED.index_membership, active=EXCLUDED.active,
  has_options=EXCLUDED.has_options, has_fundamentals=EXCLUDED.has_fundamentals,
  snapshot_24h=EXCLUDED.snapshot_24h;

-- ── Crypto (8) — 24h snapshots ────────────────────────────────────────────────
INSERT INTO universe_config (ticker, name, category, index_membership, active, has_options, has_fundamentals, snapshot_24h) VALUES
('BTC-USD',  'Bitcoin',      'crypto', ARRAY['CRYPTO'], true, false, false, true),
('ETH-USD',  'Ethereum',     'crypto', ARRAY['CRYPTO'], true, false, false, true),
('SOL-USD',  'Solana',       'crypto', ARRAY['CRYPTO'], true, false, false, true),
('BNB-USD',  'BNB',          'crypto', ARRAY['CRYPTO'], true, false, false, true),
('XRP-USD',  'XRP',          'crypto', ARRAY['CRYPTO'], true, false, false, true),
('ADA-USD',  'Cardano',      'crypto', ARRAY['CRYPTO'], true, false, false, true),
('AVAX-USD', 'Avalanche',    'crypto', ARRAY['CRYPTO'], true, false, false, true),
('DOGE-USD', 'Dogecoin',     'crypto', ARRAY['CRYPTO'], true, false, false, true)
ON CONFLICT (ticker) DO UPDATE SET
  name=EXCLUDED.name, category=EXCLUDED.category,
  index_membership=EXCLUDED.index_membership, active=EXCLUDED.active,
  has_options=EXCLUDED.has_options, has_fundamentals=EXCLUDED.has_fundamentals,
  snapshot_24h=EXCLUDED.snapshot_24h;

-- ── Commodities futures (7) ───────────────────────────────────────────────────
INSERT INTO universe_config (ticker, name, category, index_membership, active, has_options, has_fundamentals, snapshot_24h) VALUES
('GC=F',  'Gold Futures',         'commodity', ARRAY['COMMODITIES'], true, false, false, false),
('SI=F',  'Silver Futures',       'commodity', ARRAY['COMMODITIES'], true, false, false, false),
('CL=F',  'Crude Oil Futures',    'commodity', ARRAY['COMMODITIES'], true, false, false, false),
('NG=F',  'Natural Gas Futures',  'commodity', ARRAY['COMMODITIES'], true, false, false, false),
('HG=F',  'Copper Futures',       'commodity', ARRAY['COMMODITIES'], true, false, false, false),
('ZC=F',  'Corn Futures',         'commodity', ARRAY['COMMODITIES'], true, false, false, false),
('ZW=F',  'Wheat Futures',        'commodity', ARRAY['COMMODITIES'], true, false, false, false)
ON CONFLICT (ticker) DO UPDATE SET
  name=EXCLUDED.name, category=EXCLUDED.category,
  index_membership=EXCLUDED.index_membership, active=EXCLUDED.active,
  has_options=EXCLUDED.has_options, has_fundamentals=EXCLUDED.has_fundamentals,
  snapshot_24h=EXCLUDED.snapshot_24h;

-- ── Forex (6) ─────────────────────────────────────────────────────────────────
INSERT INTO universe_config (ticker, name, category, index_membership, active, has_options, has_fundamentals, snapshot_24h) VALUES
('EURUSD=X', 'EUR/USD',          'forex', ARRAY['FOREX'], true, false, false, false),
('GBPUSD=X', 'GBP/USD',          'forex', ARRAY['FOREX'], true, false, false, false),
('USDJPY=X', 'USD/JPY',          'forex', ARRAY['FOREX'], true, false, false, false),
('USDCNH=X', 'USD/CNH',          'forex', ARRAY['FOREX'], true, false, false, false),
('AUDUSD=X', 'AUD/USD',          'forex', ARRAY['FOREX'], true, false, false, false),
('DX-Y.NYB', 'US Dollar Index',  'forex', ARRAY['FOREX'], true, false, false, false)
ON CONFLICT (ticker) DO UPDATE SET
  name=EXCLUDED.name, category=EXCLUDED.category,
  index_membership=EXCLUDED.index_membership, active=EXCLUDED.active,
  has_options=EXCLUDED.has_options, has_fundamentals=EXCLUDED.has_fundamentals,
  snapshot_24h=EXCLUDED.snapshot_24h;

-- ── pipeline_config: crypto snapshot interval ─────────────────────────────────
INSERT INTO pipeline_config (key, value, description) VALUES
  ('collect_market_prices', 'true', 'Collect price history for non-equity instruments (indices/ETFs/crypto/commodities/forex)'),
  ('collect_market_technicals', 'true', 'Compute technicals from price data for non-equity instruments')
ON CONFLICT (key) DO NOTHING;
