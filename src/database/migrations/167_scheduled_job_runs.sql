-- 167: scheduled_job_runs — run accounting for systemd timers and in-process crons.
-- Pattern lifted from TimescaleDB's job-stat table (NOT its backoff/retry).
-- invocation_id ($INVOCATION_ID) pairs a systemd finish with its start.
-- Written by src/lib/job_stat.py (never raises; kill switch OPENCLAW_JOB_STAT=0).
CREATE TABLE IF NOT EXISTS scheduled_job_runs (
  id            BIGSERIAL PRIMARY KEY,
  job_name      TEXT NOT NULL,
  source        TEXT NOT NULL CHECK (source IN ('systemd','cron')),
  started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at   TIMESTAMPTZ,
  success       BOOLEAN,
  rc            INT,
  host          TEXT,
  pid           INT,
  invocation_id TEXT,
  note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_scheduled_job_runs_job
  ON scheduled_job_runs (job_name, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_scheduled_job_runs_open
  ON scheduled_job_runs (job_name, invocation_id) WHERE finished_at IS NULL;
