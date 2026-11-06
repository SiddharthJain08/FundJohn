-- 168_crypto_flow_tables.sql
-- Crypto flow data engine (Phase 1): per-ticker hourly feature snapshots,
-- per-provider health log, and a 7-day live cache of whale events.
--
-- Discipline (CLAUDE.md NEVER-DELETE): crypto_flow_snapshot and
-- crypto_flow_provider_health are append-only; never updated/deleted.
-- EXCEPTION: crypto_whale_events_live is a 7-day CACHE of the archive
-- data/master/crypto_whale_events.parquet (append-only, the source of truth);
-- pruning it loses nothing.

CREATE TABLE IF NOT EXISTS crypto_flow_snapshot (
    ticker     TEXT        NOT NULL,
    ts_utc     TIMESTAMPTZ NOT NULL,
    features   JSONB       NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_crypto_flow_snapshot_ts ON crypto_flow_snapshot (ts_utc DESC);

CREATE TABLE IF NOT EXISTS crypto_flow_provider_health (
    id         BIGSERIAL   PRIMARY KEY,
    provider   TEXT        NOT NULL,
    ts_utc     TIMESTAMPTZ NOT NULL,
    ok         BOOLEAN     NOT NULL,
    rows       INTEGER     NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    err        TEXT,
    skipped    TEXT
);
CREATE INDEX IF NOT EXISTS idx_crypto_flow_provider_health_pt ON crypto_flow_provider_health (provider, ts_utc DESC);

CREATE TABLE IF NOT EXISTS crypto_whale_events_live (   -- 7-day CACHE of data/master/crypto_whale_events.parquet (the archive)
    event_key  TEXT        PRIMARY KEY,
    ts_utc     TIMESTAMPTZ NOT NULL,
    source     TEXT,
    chain      TEXT,
    symbol     TEXT,
    ticker     TEXT,
    amount     NUMERIC,
    amount_usd NUMERIC,
    from_owner TEXT,
    to_owner   TEXT,
    direction  TEXT,
    tx_hash    TEXT
);
CREATE INDEX IF NOT EXISTS idx_crypto_whale_events_live_ts ON crypto_whale_events_live (ts_utc DESC);
