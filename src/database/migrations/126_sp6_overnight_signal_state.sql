-- 126: SP-6 overnight signal state schema.
-- Adds lifecycle tracking columns to execution_signals, plus gate verdict audit log
-- and EOD compute health monitoring.
-- All columns on execution_signals are nullable (additive); UNIQUE constraint unchanged.
-- signal_gate_verdicts and eod_compute_health are new append-only tables.

-- 1. Add lifecycle columns to execution_signals (nullable, additive, NO DEFAULT)
ALTER TABLE execution_signals
    ADD COLUMN IF NOT EXISTS lifecycle_state TEXT,
    ADD COLUMN IF NOT EXISTS target_date DATE,
    ADD COLUMN IF NOT EXISTS computed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS executing_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS filled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS gate_verdict JSONB,
    ADD COLUMN IF NOT EXISTS fill_price NUMERIC,
    ADD COLUMN IF NOT EXISTS mark_entry_price NUMERIC;

-- 2. Create signal_gate_verdicts table (one row per gate decision audit)
CREATE TABLE IF NOT EXISTS signal_gate_verdicts (
    id BIGSERIAL PRIMARY KEY,
    signal_id UUID, -- no FK: audit rows must survive signal deletion
    gate_type TEXT,
    ticker TEXT,
    target_date DATE,
    verdict TEXT,
    panic_score NUMERIC,
    news_count INT,
    severity INT,
    model TEXT,
    metadata JSONB,
    actor TEXT,
    decided_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS signal_gate_verdicts_target_date_ticker
    ON signal_gate_verdicts(target_date, ticker);

-- 3. Create eod_compute_health table (one row per daily EOD compute run)
CREATE TABLE IF NOT EXISTS eod_compute_health (
    id BIGSERIAL PRIMARY KEY,
    run_date DATE,
    run_at TIMESTAMPTZ DEFAULT NOW(),
    rc INT,
    n_strategies_ok INT,
    n_strategies_total INT,
    regime_ok BOOLEAN,
    universe_size INT,
    healthy BOOLEAN,
    detail JSONB
);

CREATE INDEX IF NOT EXISTS eod_compute_health_run_date_desc
    ON eod_compute_health(run_date DESC);
