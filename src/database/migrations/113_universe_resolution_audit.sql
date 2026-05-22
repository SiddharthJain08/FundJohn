-- 113_universe_resolution_audit.sql
-- Lightweight audit: every union_universe call gets a row so the dashboard
-- can show drift. NOT every per-strategy resolve (too noisy); only the
-- daily union.

CREATE TABLE IF NOT EXISTS universe_resolution_audit (
  id                  BIGSERIAL PRIMARY KEY,
  resolved_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_for_date   DATE NOT NULL,
  lifecycle_states    TEXT[] NOT NULL,
  union_size          INT NOT NULL,
  per_strategy_sizes  JSONB NOT NULL,
  alpaca_universe_size INT NOT NULL,                -- denominator for "inflation" stat
  resolver_ms         INT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uni_audit_date ON universe_resolution_audit(resolved_for_date DESC);
