-- Phase 1F follow-up #13 — split DBnomics per-series metadata out of the
-- per-observation table, and add a sortable period_start DATE column for
-- clean cross-frequency range filtering.
--
-- Background:
--   Migration 094 created dbnomics_observations with `raw_payload JSONB NOT NULL`
--   per-observation, which duplicated the (heavy) DBnomics doc envelope across
--   every period of every series — a 60-month CPI series wrote the same envelope
--   60 times. This migration moves the envelope to a new dbnomics_series table
--   (one row per series), and lets new observation inserts leave the legacy
--   per-row raw_payload column NULL.
--
--   It also adds period_start DATE + frequency TEXT to dbnomics_observations so
--   queries can range-filter cleanly across mixed frequencies (the existing
--   `period TEXT` field doesn't sort coherently across '2026', '2026-Q1',
--   '2026-01', '2026-01-15').
--
-- Append-only per CLAUDE.md core invariant: no DROP / DELETE. Existing rows
-- (currently 0) are preserved with their raw_payload intact and series_id /
-- period_start / frequency NULL.
--
-- Recommended consumer query going forward:
--
--   SELECT o.period, o.period_start, o.value, s.name, s.frequency
--   FROM dbnomics_observations o
--   JOIN dbnomics_series s
--     ON  o.provider_code = s.provider_code
--     AND o.dataset_code  = s.dataset_code
--     AND o.series_code   = s.series_code
--   WHERE o.period_start >= '2024-01-01'
--     AND o.period_start <  '2025-01-01'
--   ORDER BY o.period_start;

CREATE TABLE IF NOT EXISTS dbnomics_series (
    id                BIGSERIAL PRIMARY KEY,
    provider_code     TEXT NOT NULL,
    dataset_code      TEXT NOT NULL,
    series_code       TEXT NOT NULL,
    name              TEXT,           -- doc.series_name when available
    frequency         TEXT,           -- doc.@frequency ('monthly' / 'quarterly' / 'annual' / ...)
    raw_payload       JSONB NOT NULL, -- the full doc envelope, ONE copy per series
    first_fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider_code, dataset_code, series_code)
);

CREATE INDEX IF NOT EXISTS idx_dbnomics_series_lookup
    ON dbnomics_series (provider_code, dataset_code, series_code);

-- Skinny up dbnomics_observations: add FK + sortable period + frequency.
ALTER TABLE dbnomics_observations
    ADD COLUMN IF NOT EXISTS series_id    BIGINT REFERENCES dbnomics_series(id),
    ADD COLUMN IF NOT EXISTS period_start DATE,
    ADD COLUMN IF NOT EXISTS frequency    TEXT;

-- Drop the NOT NULL on the legacy raw_payload column so new inserts can
-- leave it NULL (the envelope now lives in dbnomics_series). Naturally
-- idempotent — DROP NOT NULL on an already-nullable column is a no-op.
ALTER TABLE dbnomics_observations
    ALTER COLUMN raw_payload DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dbnomics_obs_period_start
    ON dbnomics_observations (period_start);

CREATE INDEX IF NOT EXISTS idx_dbnomics_obs_series_id
    ON dbnomics_observations (series_id);
