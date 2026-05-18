-- Phase 1F — spike capture for DBnomics + Polymarket.
-- Append-only per existing memory rule (NEVER delete from master DB).

CREATE TABLE IF NOT EXISTS dbnomics_observations (
    id              BIGSERIAL PRIMARY KEY,
    provider_code   TEXT NOT NULL,
    dataset_code    TEXT NOT NULL,
    series_code     TEXT NOT NULL,
    period          TEXT NOT NULL,
    value           DOUBLE PRECISION,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_payload     JSONB NOT NULL,
    UNIQUE (provider_code, dataset_code, series_code, period)
);
CREATE INDEX IF NOT EXISTS idx_dbnomics_obs_series ON dbnomics_observations (provider_code, dataset_code, series_code);
CREATE INDEX IF NOT EXISTS idx_dbnomics_obs_period ON dbnomics_observations (period);

CREATE TABLE IF NOT EXISTS polymarket_market_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL,
    question        TEXT NOT NULL,
    end_date        TIMESTAMPTZ,
    yes_price       DOUBLE PRECISION,
    no_price        DOUBLE PRECISION,
    volume_24h_usd  DOUBLE PRECISION,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_polymarket_snap_market ON polymarket_market_snapshots (market_id, fetched_at DESC);
