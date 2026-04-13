-- Migration 020: Drop technicals table and purge coverage records.
-- Technicals (RSI-14, SMA-20/50/200) were computed from stored prices but never
-- consumed by any live strategy. The collection phase was causing 4+ hours of
-- Polygon 429 retries per night on the Options Starter plan.
DROP TABLE IF EXISTS technicals;
DELETE FROM data_coverage WHERE data_type = 'technicals';
