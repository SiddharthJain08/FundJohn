-- Migration 021: Add quality/value ratio columns to fundamentals table.
-- These are fetched from FMP /stable/ratios and consumed by S10_quality_value strategy.
ALTER TABLE fundamentals
    ADD COLUMN IF NOT EXISTS roe              NUMERIC,
    ADD COLUMN IF NOT EXISTS roic             NUMERIC,
    ADD COLUMN IF NOT EXISTS debt_equity_ratio NUMERIC,
    ADD COLUMN IF NOT EXISTS p_fcf_ratio      NUMERIC;
-- Note: ev_ebitda and gross_margin already exist in this table.
