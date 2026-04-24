-- 053_performance_outliers.sql
-- Persistent per-cycle record of d-1 performance outliers.
-- Row written by trade_handoff_builder.py::build() each cycle for every
-- position that cleared the |σΔ| gate on yesterday's outcome. Used to
-- compute cumulative O/U counts on the strategies dashboard (the paired
-- R count comes from veto_log). UNIQUE constraint prevents double-
-- counting when a handoff is rebuilt.

CREATE TABLE IF NOT EXISTS performance_outliers (
    id               BIGSERIAL PRIMARY KEY,
    cycle_date       DATE        NOT NULL,
    strategy_id      TEXT        NOT NULL,
    ticker           TEXT,
    kind             TEXT        NOT NULL CHECK (kind IN ('over', 'under')),
    sigma_delta      NUMERIC,
    delta            NUMERIC,
    realized_pct     NUMERIC,
    unrealized_pct   NUMERIC,
    ev_gbm           NUMERIC,
    days_held        INTEGER,
    status           TEXT,
    close_reason     TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (cycle_date, strategy_id, ticker, kind)
);

CREATE INDEX IF NOT EXISTS idx_perf_outliers_strategy ON performance_outliers(strategy_id, kind);
CREATE INDEX IF NOT EXISTS idx_perf_outliers_cycle    ON performance_outliers(cycle_date DESC);
