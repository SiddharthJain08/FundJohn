-- Migration 106: ticker_sentiment_daily — per (ticker, date) social + news sentiment rollup.
-- Fed by scripts/run_sentiment_step.py daily. Consumed by trade_handoff_builder.py
-- to enrich tradejohn_confirmer proposals.

CREATE TABLE IF NOT EXISTS ticker_sentiment_daily (
  ticker                  TEXT NOT NULL,
  date                    DATE NOT NULL,
  -- social (Reddit + StockTwits aggregate)
  social_posts_24h        INT     NOT NULL DEFAULT 0,
  social_bull_ratio       NUMERIC,
  social_bear_ratio       NUMERIC,
  social_unique_authors   INT     NOT NULL DEFAULT 0,
  social_top_themes       JSONB,
  -- news (FinBERT-scored over market_news rows)
  news_count_24h          INT     NOT NULL DEFAULT 0,
  news_finbert_pos        INT     NOT NULL DEFAULT 0,
  news_finbert_neu        INT     NOT NULL DEFAULT 0,
  news_finbert_neg        INT     NOT NULL DEFAULT 0,
  news_mean_score         NUMERIC,  -- signed: +1 = fully positive, -1 = fully negative
  news_top_headlines      JSONB,    -- top 3 by |polarity|
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_sentiment_date ON ticker_sentiment_daily(date);
