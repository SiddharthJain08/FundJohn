-- Migration 019: Restore sma_20 column to technicals table.
ALTER TABLE technicals ADD COLUMN IF NOT EXISTS sma_20 numeric;
