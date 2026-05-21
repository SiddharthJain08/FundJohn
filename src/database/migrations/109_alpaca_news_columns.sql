-- 109_alpaca_news_columns.sql
-- SP-1: extend ticker_sentiment_daily with Alpaca News API columns
-- Append-only; no drops.

BEGIN;

ALTER TABLE ticker_sentiment_daily
  ADD COLUMN IF NOT EXISTS alpaca_news_count_24h INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS alpaca_news_finbert_pos INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS alpaca_news_finbert_neu INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS alpaca_news_finbert_neg INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS alpaca_news_mean_score NUMERIC,
  ADD COLUMN IF NOT EXISTS alpaca_news_top_headlines JSONB;

COMMENT ON COLUMN ticker_sentiment_daily.alpaca_news_count_24h IS
  'SP-1: count of Alpaca news articles for this ticker in trailing 24h';
COMMENT ON COLUMN ticker_sentiment_daily.alpaca_news_mean_score IS
  'SP-1: FinBERT signed mean score [-1, 1] over Alpaca news for trailing 24h';

COMMIT;
