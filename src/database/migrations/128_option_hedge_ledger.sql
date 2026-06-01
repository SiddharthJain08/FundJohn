-- 128_option_hedge_ledger.sql — SP-5.1b-ii delta-hedge ledger.
-- Operational mutable state (NOT master/append-only). Sole source of truth for
-- the hedge share position per option strategy; hedging never reads the total
-- broker book, so equity strategies on the same ticker are structurally invisible.
CREATE TABLE IF NOT EXISTS option_hedge_ledger (
    id                 BIGSERIAL PRIMARY KEY,
    option_strategy_id TEXT NOT NULL,
    underlying         TEXT NOT NULL,
    structure_legs     JSONB NOT NULL,
    contracts          NUMERIC NOT NULL,
    current_hedge_qty  NUMERIC NOT NULL DEFAULT 0,
    target_hedge_qty   NUMERIC,
    last_rehedge_date  DATE,
    status             TEXT NOT NULL DEFAULT 'active',
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (option_strategy_id, underlying)
);
CREATE INDEX IF NOT EXISTS idx_option_hedge_ledger_active
    ON option_hedge_ledger (status) WHERE status = 'active';
