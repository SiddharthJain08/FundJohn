-- src/database/migrations/121_edgar_8k_filings.sql
-- EDGAR 8-K filings, one row per Item.
-- Companion to migration 120 (premarket_panic_alerts).

CREATE TABLE IF NOT EXISTS edgar_8k_filings (
    id                  BIGSERIAL PRIMARY KEY,
    accession           TEXT NOT NULL,
    cik                 TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    filing_date         DATE NOT NULL,
    accepted_at         TIMESTAMPTZ,
    item_number         TEXT NOT NULL,
    item_description    TEXT NOT NULL,
    primary_doc_url     TEXT,
    market_news_uuid    TEXT,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (accession, item_number)
);

CREATE INDEX IF NOT EXISTS edgar_8k_filings_ticker_date
    ON edgar_8k_filings(ticker, filing_date DESC);
CREATE INDEX IF NOT EXISTS edgar_8k_filings_item_number
    ON edgar_8k_filings(item_number, filing_date DESC);
CREATE INDEX IF NOT EXISTS edgar_8k_filings_accession
    ON edgar_8k_filings(accession);
