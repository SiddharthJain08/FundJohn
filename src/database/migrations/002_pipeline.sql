-- OpenClaw Pipeline Schema

-- Universe of tracked equities
CREATE TABLE IF NOT EXISTS universe (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    sector TEXT,
    industry TEXT,
    market_cap NUMERIC,
    index_membership TEXT[],
    active BOOLEAN DEFAULT TRUE,
    added_at TIMESTAMPTZ DEFAULT NOW(),
    last_updated TIMESTAMPTZ
);

-- Daily OHLCV price data
CREATE TABLE IF NOT EXISTS price_data (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    open NUMERIC,
    high NUMERIC,
    low NUMERIC,
    close NUMERIC,
    volume BIGINT,
    vwap NUMERIC,
    transactions INTEGER,
    source TEXT DEFAULT 'polygon',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(ticker, date)
);

-- Intraday snapshots (refreshed during market hours)
CREATE TABLE IF NOT EXISTS snapshots (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL,
    price NUMERIC,
    change_pct NUMERIC,
    volume BIGINT,
    vwap NUMERIC,
    day_high NUMERIC,
    day_low NUMERIC,
    prev_close NUMERIC,
    market_cap NUMERIC,
    source TEXT DEFAULT 'polygon',
    UNIQUE(ticker, snapshot_at)
);

-- Options chain with Greeks (from Polygon)
CREATE TABLE IF NOT EXISTS options_data (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    expiry DATE NOT NULL,
    strike NUMERIC NOT NULL,
    contract_type TEXT NOT NULL,   -- 'call' | 'put'
    delta NUMERIC,
    gamma NUMERIC,
    theta NUMERIC,
    vega NUMERIC,
    rho NUMERIC,
    iv NUMERIC,                    -- implied volatility
    open_interest INTEGER,
    volume INTEGER,
    last_price NUMERIC,
    bid NUMERIC,
    ask NUMERIC,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(ticker, snapshot_date, expiry, strike, contract_type)
);

-- Technical indicators (daily)
CREATE TABLE IF NOT EXISTS technicals (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    rsi_14 NUMERIC,
    sma_20 NUMERIC,
    sma_50 NUMERIC,
    sma_200 NUMERIC,
    ema_12 NUMERIC,
    ema_26 NUMERIC,
    macd NUMERIC,
    macd_signal NUMERIC,
    bb_upper NUMERIC,
    bb_middle NUMERIC,
    bb_lower NUMERIC,
    atr_14 NUMERIC,
    source TEXT DEFAULT 'polygon',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(ticker, date)
);

-- Fundamental metrics (quarterly)
CREATE TABLE IF NOT EXISTS fundamentals (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    period TEXT NOT NULL,          -- e.g. '2025Q4'
    period_end DATE NOT NULL,
    revenue NUMERIC,
    gross_profit NUMERIC,
    ebitda NUMERIC,
    net_income NUMERIC,
    eps NUMERIC,
    gross_margin NUMERIC,
    operating_margin NUMERIC,
    net_margin NUMERIC,
    revenue_growth_yoy NUMERIC,
    ev_revenue NUMERIC,
    ev_ebitda NUMERIC,
    pe_ratio NUMERIC,
    market_cap NUMERIC,
    source TEXT DEFAULT 'fmp',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(ticker, period)
);

-- Pipeline collection log (audit trail + status)
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT,                   -- NULL = universe-wide run
    run_type TEXT NOT NULL,        -- 'snapshot', 'prices', 'options', 'technicals', 'fundamentals'
    status TEXT NOT NULL,          -- 'success', 'error', 'skipped', 'partial'
    records_written INTEGER DEFAULT 0,
    error_message TEXT,
    duration_ms INTEGER,
    api_calls_used INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_price_data_ticker_date ON price_data(ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_price_data_date ON price_data(date DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker ON snapshots(ticker, snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_options_ticker_expiry ON options_data(ticker, snapshot_date, expiry);
CREATE INDEX IF NOT EXISTS idx_technicals_ticker_date ON technicals(ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_fundamentals_ticker ON fundamentals(ticker, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_type ON pipeline_runs(run_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_universe_active ON universe(active);
