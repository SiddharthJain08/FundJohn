CREATE TABLE IF NOT EXISTS veto_log (
    id           BIGSERIAL PRIMARY KEY,
    run_date     DATE NOT NULL,
    strategy_id  TEXT REFERENCES strategy_registry(id),
    ticker       TEXT,
    veto_reason  TEXT NOT NULL,
    field_name   TEXT,
    ev           NUMERIC,
    kelly        NUMERIC,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_veto_log_date     ON veto_log(run_date DESC);
CREATE INDEX IF NOT EXISTS idx_veto_log_strategy ON veto_log(strategy_id, run_date DESC);
CREATE INDEX IF NOT EXISTS idx_veto_log_reason   ON veto_log(veto_reason);
