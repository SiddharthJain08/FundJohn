-- Migration 017: insider_transactions table for Form 4 SEC filings
-- Feeds insider.parquet via sync_master_parquets.py → InsiderClusterBuy strategy

CREATE TABLE IF NOT EXISTS insider_transactions (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT        NOT NULL,
    filing_date         DATE        NOT NULL,
    transaction_date    DATE,
    insider_name        TEXT,
    role                TEXT,       -- 'director', 'officer', 'CEO', '10% owner', etc.
    transaction_type    TEXT,       -- 'P-Purchase', 'S-Sale', 'A-Grant', etc.
    shares              NUMERIC,
    price_per_share     NUMERIC,
    total_value         NUMERIC,    -- shares * price
    shares_owned_after  NUMERIC,
    source              TEXT DEFAULT 'fmp',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (ticker, filing_date, insider_name, transaction_type, shares)
);

CREATE INDEX IF NOT EXISTS idx_insider_ticker          ON insider_transactions (ticker);
CREATE INDEX IF NOT EXISTS idx_insider_filing_date     ON insider_transactions (filing_date DESC);
CREATE INDEX IF NOT EXISTS idx_insider_ticker_date     ON insider_transactions (ticker, filing_date DESC);
CREATE INDEX IF NOT EXISTS idx_insider_transaction_type ON insider_transactions (transaction_type);
