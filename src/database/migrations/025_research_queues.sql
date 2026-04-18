-- Paper candidates queue — one row per submitted paper URL
CREATE TABLE research_candidates (
  candidate_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_url          TEXT NOT NULL,
  submitted_by        TEXT NOT NULL DEFAULT 'operator',
  submitted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  priority            INTEGER NOT NULL DEFAULT 5,
  status              TEXT NOT NULL DEFAULT 'pending',
  hunter_result_json  JSONB,
  kind                TEXT NOT NULL DEFAULT 'paper'
);

-- Data column requests: BUILDABLE papers that need a new column wired
CREATE TABLE data_ingestion_queue (
  request_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  requested_by_candidate_id     UUID REFERENCES research_candidates,
  column_name                   TEXT NOT NULL,
  transform_spec                JSONB,
  provider_preferred            TEXT,
  provider_fallback             TEXT,
  refresh_cadence               TEXT,
  estimated_cost_per_month      NUMERIC,
  status                        TEXT NOT NULL DEFAULT 'PENDING',
  priority                      INTEGER NOT NULL DEFAULT 5,
  requested_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  approved_by                   TEXT,
  approved_at                   TIMESTAMPTZ,
  wired_at                      TIMESTAMPTZ,
  failure_reason                TEXT
);

-- Column deprecation requests: reaper identifies orphaned columns
CREATE TABLE data_deprecation_queue (
  request_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  column_name                   TEXT NOT NULL,
  last_used_by                  TEXT,
  deprecated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  provider_monthly_cost_saved   NUMERIC,
  recommended_action            TEXT,
  reversible_for_days           INTEGER NOT NULL DEFAULT 30,
  status                        TEXT NOT NULL DEFAULT 'PENDING',
  approved_by                   TEXT,
  approved_at                   TIMESTAMPTZ
);

-- Master column registry: one row per tracked data column
CREATE TABLE data_columns (
  column_name                       TEXT PRIMARY KEY,
  provider                          TEXT NOT NULL,
  introduced_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  introduced_by_strategy_candidate  UUID REFERENCES research_candidates,
  refresh_cadence                   TEXT NOT NULL DEFAULT 'daily',
  estimated_monthly_cost            NUMERIC NOT NULL DEFAULT 0,
  last_consumed_at                  TIMESTAMPTZ
);

-- Thin audit: which strategy read which column on which date
CREATE TABLE signal_cache_reads (
  id                  BIGSERIAL PRIMARY KEY,
  column_name         TEXT NOT NULL,
  read_date           DATE NOT NULL DEFAULT CURRENT_DATE,
  reader_strategy_id  TEXT REFERENCES strategy_registry(id),
  read_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX signal_cache_reads_col_date_idx ON signal_cache_reads (column_name, read_date);

-- READY papers waiting for StrategyCoder
CREATE TABLE implementation_queue (
  item_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id   UUID REFERENCES research_candidates,
  strategy_spec  JSONB NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending',
  queued_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  coded_at       TIMESTAMPTZ
);

-- Lifecycle transition audit trail
CREATE TABLE lifecycle_events (
  event_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id  TEXT NOT NULL REFERENCES strategy_registry(id),
  from_state   TEXT NOT NULL,
  to_state     TEXT NOT NULL,
  actor        TEXT NOT NULL,
  reason       TEXT,
  metadata     JSONB,
  occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
