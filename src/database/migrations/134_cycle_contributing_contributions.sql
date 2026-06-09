-- 2026-06-09: per-cycle, per-ticker SIGNED alpha contributions.
-- Extends migration 130 (cycle_contributing_strategies). For each corr-gate
-- representative the sizer keeps for a ticker, store its actual signed
-- allocation term daily_weight × size_scalar × direction (sharpe units; the
-- exact value summed into ticker_w). Read by /api/portfolio/ticker-alpha to
-- show signed contributions on open + closed positions. Additive only — the
-- existing `strategies TEXT[]` column is preserved for back-compat fallback.
ALTER TABLE cycle_contributing_strategies
    ADD COLUMN IF NOT EXISTS contributions JSONB;
