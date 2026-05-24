-- 117_lifecycle_audit_log.sql
-- Append-only audit trail for lifecycle events (universe adoption, revert, etc.)
-- Master invariant: rows append-only; never DELETE or UPDATE.

CREATE TABLE IF NOT EXISTS lifecycle_audit_log (
  id            BIGSERIAL PRIMARY KEY,
  event         TEXT NOT NULL,
  strategy_id   TEXT NOT NULL,
  before_state  TEXT,
  after_state   TEXT,
  actor         TEXT NOT NULL,
  occurred_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lcal_strategy ON lifecycle_audit_log(strategy_id, occurred_at DESC);
