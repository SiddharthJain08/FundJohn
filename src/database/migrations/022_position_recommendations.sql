CREATE TABLE IF NOT EXISTS position_recommendations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_date            DATE NOT NULL,
    ticker              TEXT NOT NULL,
    strategy_id         TEXT REFERENCES strategy_registry(id),
    action              TEXT NOT NULL CHECK (action IN ('EXIT_EARLY','INCREASE_SIZE','REDUCE_SIZE','HOLD')),
    rationale           TEXT NOT NULL,
    entry_price         NUMERIC NOT NULL,
    current_price       NUMERIC NOT NULL,
    unrealized_pnl_pct  NUMERIC NOT NULL,
    days_held           INTEGER NOT NULL,
    max_hp_days         INTEGER NOT NULL,
    stop_loss           NUMERIC,
    profit_target       NUMERIC,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','approved','rejected','expired')),
    resolved_at         TIMESTAMPTZ,
    resolved_by         TEXT,
    alpaca_order_id     TEXT,
    alpaca_status       TEXT,
    alpaca_error        TEXT,
    discord_message_id  TEXT,
    discord_channel_id  TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(run_date, ticker, strategy_id, action)
);

CREATE INDEX IF NOT EXISTS idx_position_recs_date   ON position_recommendations(run_date DESC);
CREATE INDEX IF NOT EXISTS idx_position_recs_status ON position_recommendations(status);

UPDATE agent_registry
SET channel_keys = array_append(channel_keys, 'position-recommendations')
WHERE id = 'tradedesk'
  AND NOT ('position-recommendations' = ANY(channel_keys));
