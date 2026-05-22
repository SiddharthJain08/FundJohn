-- 115_backfill_audit.sql
--
-- Append-only durable audit log of every backfill chunk attempt.
-- Redis (`backfill:*` keys) is for ops orchestration and progress tracking;
-- this table is for forensics + audit and survives a Redis flush.
--
-- Every chunk attempted by the SP-2 Phase B 5y backfill (prices, metadata,
-- options) writes one row here per (target, chunk_key, source_tag, started_at)
-- tuple. Status transitions are append-only via new rows; the lifecycle is
-- in_progress -> {validated, failed} -> {promoted, quarantined}.

CREATE TABLE IF NOT EXISTS backfill_audit (
  id            BIGSERIAL PRIMARY KEY,
  target        TEXT NOT NULL,                    -- 'prices' | 'metadata' | 'options'
  chunk_key     TEXT NOT NULL,                    -- 'AAPL:2022' or '2022-08-31:metadata'
  started_at    TIMESTAMPTZ NOT NULL,
  ended_at      TIMESTAMPTZ,
  status        TEXT NOT NULL,                    -- 'in_progress'|'validated'|'promoted'|'quarantined'|'failed'
  rows_written  INTEGER,
  source_tag    TEXT NOT NULL,
  sha256        TEXT,
  error_text    TEXT,
  CONSTRAINT backfill_audit_chunk_unique UNIQUE (target, chunk_key, source_tag, started_at)
);

CREATE INDEX IF NOT EXISTS idx_backfill_audit_status ON backfill_audit(target, status);
CREATE INDEX IF NOT EXISTS idx_backfill_audit_recent ON backfill_audit(started_at DESC);
