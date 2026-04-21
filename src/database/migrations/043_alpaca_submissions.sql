-- Dedicated table for orders auto-submitted by alpaca_executor.
-- position_recommendations is semantically for existing-position decisions
-- (HOLD / EXIT_EARLY / INCREASE_SIZE / REDUCE_SIZE), not for new-order
-- submissions. Keep those concerns separate.

CREATE TABLE IF NOT EXISTS alpaca_submissions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_date          DATE NOT NULL,
  ticker            TEXT NOT NULL,
  strategy_id       TEXT NOT NULL,
  direction         TEXT NOT NULL,        -- 'long' | 'short'
  qty               INT  NOT NULL,
  entry_price       NUMERIC NOT NULL,
  stop_price        NUMERIC,
  target_price      NUMERIC,
  pct_nav           NUMERIC,
  notional_usd      NUMERIC,
  time_in_force     TEXT NOT NULL,        -- 'day' | 'opg' | 'gtc'
  order_class       TEXT NOT NULL,        -- 'bracket' | 'simple'
  client_order_id   TEXT NOT NULL UNIQUE,
  alpaca_order_id   TEXT,
  alpaca_status     TEXT,                 -- 'submitted' | 'error' | 'recovered'
  alpaca_http       INT,
  alpaca_error      TEXT,
  submitted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS alpaca_submissions_run_idx
  ON alpaca_submissions (run_date DESC, strategy_id, ticker);

CREATE UNIQUE INDEX IF NOT EXISTS alpaca_submissions_dedupe_idx
  ON alpaca_submissions (run_date, strategy_id, ticker);
