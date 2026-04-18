-- Migration 027: Universe sync support columns
-- Adds source tracking and market_cap_tier to universe_config for full-market expansion

ALTER TABLE universe_config ADD COLUMN IF NOT EXISTS source          TEXT    DEFAULT 'manual';
ALTER TABLE universe_config ADD COLUMN IF NOT EXISTS market_cap_tier TEXT    DEFAULT NULL;
ALTER TABLE universe_config ADD COLUMN IF NOT EXISTS exchange        TEXT    DEFAULT NULL;

-- Tag existing SP500/SP100 rows as FMP-sourced
UPDATE universe_config
SET source = 'fmp'
WHERE 'SP100' = ANY(index_membership) OR 'SP500' = ANY(index_membership);

-- Market cap tier helper: populated by sync_universe_to_db after fetching Polygon reference data
-- Values: 'mega' (>200B), 'large' (10B-200B), 'mid' (2B-10B), 'small' (300M-2B), 'micro' (<300M)
COMMENT ON COLUMN universe_config.market_cap_tier IS 'mega|large|mid|small|micro — populated by Polygon sync';
COMMENT ON COLUMN universe_config.source IS 'fmp|polygon|manual — data provider that sourced this row';
COMMENT ON COLUMN universe_config.exchange IS 'NASDAQ|NYSE|AMEX|XNAS|XNYS|XASE — primary listing exchange';
