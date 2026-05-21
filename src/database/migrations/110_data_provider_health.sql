-- 110_data_provider_health.sql
-- SP-1: rolling per-provider health counters surfaced on Data Health dashboard tile.
-- Append-only.

BEGIN;

CREATE TABLE IF NOT EXISTS data_provider_health (
  provider       TEXT        NOT NULL,
  endpoint       TEXT        NOT NULL,
  success_count  INT         NOT NULL DEFAULT 0,
  error_count    INT         NOT NULL DEFAULT 0,
  last_error     TEXT,
  last_error_at  TIMESTAMPTZ,
  window_start   TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (provider, endpoint, window_start)
);

CREATE INDEX IF NOT EXISTS idx_data_provider_health_recent
  ON data_provider_health (provider, window_start DESC);

COMMENT ON TABLE data_provider_health IS
  'SP-1: rolling 24h counters per (provider, endpoint, window_start). Window bucketed hourly.';

COMMIT;
