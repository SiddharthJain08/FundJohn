-- Migration 018: Remove never-populated columns from technicals table.
-- Only rsi_14, sma_50, sma_200 are collected; everything else has always been NULL.

ALTER TABLE technicals
  DROP COLUMN IF EXISTS sma_20,
  DROP COLUMN IF EXISTS ema_12,
  DROP COLUMN IF EXISTS ema_26,
  DROP COLUMN IF EXISTS macd,
  DROP COLUMN IF EXISTS macd_signal,
  DROP COLUMN IF EXISTS bb_upper,
  DROP COLUMN IF EXISTS bb_middle,
  DROP COLUMN IF EXISTS bb_lower,
  DROP COLUMN IF EXISTS atr_14;
