-- Per-strategy precomputed backtest dashboard panel (additive; never-delete-safe).
CREATE TABLE IF NOT EXISTS strategy_backtest_panel (
    strategy_id        TEXT PRIMARY KEY,
    run_id             UUID,
    effective_sharpe   DOUBLE PRECISION,
    cadence_days       DOUBLE PRECISION,
    oue_over           INTEGER,
    oue_under          INTEGER,
    oue_expected       INTEGER,
    oue_by_regime      JSONB,
    oue_sigma_gate     DOUBLE PRECISION,
    equity_curve       JSONB,
    n_trades           INTEGER,
    computed_at        TIMESTAMPTZ DEFAULT NOW()
);
