-- Migration 045: strategy_approval_jobs
--
-- Tracks async jobs kicked off by the dashboard "Approve" button on staging
-- and candidate strategies. Staging approvals register missing data sources
-- and poll data_coverage until the first complete snapshot lands; candidate
-- approvals invoke StrategyCoder + the 3-window convergence backtest and
-- promote candidate → paper on pass. One active job per strategy.
--
-- Existing queues (data_ingestion_queue, implementation_queue) still hold the
-- real work; this table is the user-facing progress ledger that the dashboard
-- streams via SSE.

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS strategy_approval_jobs (
  job_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id  TEXT NOT NULL REFERENCES strategy_registry(id),
  kind         TEXT NOT NULL CHECK (kind IN ('approve_staging','approve_candidate')),
  status       TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
  phase        TEXT,
  progress     INT NOT NULL DEFAULT 0,
  payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
  result       JSONB,
  actor        TEXT NOT NULL,
  started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at  TIMESTAMPTZ,
  CONSTRAINT one_active_job_per_strategy
    EXCLUDE USING gist (strategy_id WITH =)
    WHERE (status IN ('pending','running'))
);

CREATE INDEX IF NOT EXISTS idx_approval_jobs_active
  ON strategy_approval_jobs (status, started_at)
  WHERE status IN ('pending','running');

CREATE INDEX IF NOT EXISTS idx_approval_jobs_strategy
  ON strategy_approval_jobs (strategy_id, started_at DESC);
