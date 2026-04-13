-- Migration 009: Market news storage
-- Stores articles collected via yfinance for all universe instruments.
-- Deduped by article UUID. Pruned after 30 days.

CREATE TABLE IF NOT EXISTS market_news (
  id           BIGSERIAL PRIMARY KEY,
  uuid         TEXT UNIQUE NOT NULL,
  primary_ticker TEXT,                   -- instrument whose news feed surfaced this article
  title        TEXT NOT NULL,
  publisher    TEXT,
  url          TEXT,
  published_at TIMESTAMPTZ,
  summary      TEXT,
  related_tickers TEXT[] DEFAULT '{}',
  fetched_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_news_published ON market_news(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_ticker    ON market_news(primary_ticker);
CREATE INDEX IF NOT EXISTS idx_news_related   ON market_news USING GIN(related_tickers);
