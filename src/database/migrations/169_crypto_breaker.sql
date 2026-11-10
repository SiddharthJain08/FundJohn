-- 169_crypto_breaker.sql
-- Crypto breaker (Phase 2): per-tick evaluation log + asset_class on the
-- liquidation audit table.
--
-- Discipline (CLAUDE.md NEVER-DELETE): crypto_breaker_evals is append-only;
-- never updated/deleted. alpaca_liquidations only gains a nullable column.

CREATE TABLE IF NOT EXISTS crypto_breaker_evals (
    id                  BIGSERIAL PRIMARY KEY,
    ts_utc              TIMESTAMPTZ NOT NULL,
    mode                TEXT        NOT NULL CHECK (mode IN ('shadow','armed','dry_run')),
    n_positions         INT         NOT NULL,
    sleeve_cost_basis   NUMERIC,
    sleeve_unrealized_pl NUMERIC,
    sleeve_dd           NUMERIC,
    worst_ticker        TEXT,
    worst_plpc          NUMERIC,
    triggers            JSONB       NOT NULL DEFAULT '[]',
    action              TEXT        NOT NULL,
    skipped             TEXT,
    hold_flat           BOOLEAN     NOT NULL DEFAULT FALSE,
    note                TEXT
);
CREATE INDEX IF NOT EXISTS idx_crypto_breaker_evals_ts ON crypto_breaker_evals (ts_utc DESC);

ALTER TABLE alpaca_liquidations ADD COLUMN IF NOT EXISTS asset_class TEXT;
