-- 093_unified_backtest_tables.sql
--
-- Single source of truth for strategy backtests. Replaces the
-- coexisting paths: quick_backtest.py (dashboard draft), auto_backtest.py
-- (research-orch promotion), strategy_regime_backtests (sizer auto-demote),
-- strategy_registry.backtest_* JSON column.
--
-- Methodology: discovery — run the strategy across full historical_regimes
-- coverage (2016-04 → today), tag every simulated trade with the regime
-- live on its entry date, aggregate per-regime to discover where the
-- strategy actually works. The discovered metrics populate
-- strategies.metadata.eligible_regimes automatically (eligibility_assigner.py).
--
-- Append-only per master-data invariant. Each run_id snapshots one
-- (strategy, code state) at one point in time; rerunning never UPDATES
-- prior rows, only inserts new ones with a fresh run_id. Consumers read
-- the latest run_id per strategy via primary_window=true sentinel.

CREATE TABLE IF NOT EXISTS strategy_backtest_runs (
  run_id            UUID PRIMARY KEY,
  strategy_id       TEXT NOT NULL,
  code_sha          TEXT,                       -- git rev of implementation file at run-time, or content hash if dirty
  run_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  window_kind       TEXT NOT NULL DEFAULT 'full_history',  -- reserved for future ('dense_modern' if operator flips)
  start_date        DATE NOT NULL,
  end_date          DATE NOT NULL,
  oos_days          INT NOT NULL,
  total_sharpe      DOUBLE PRECISION,
  total_max_dd_pct  DOUBLE PRECISION,           -- positive number, % drawdown magnitude
  total_return_pct  DOUBLE PRECISION,
  total_trades      INT NOT NULL,
  total_hit_rate    DOUBLE PRECISION,           -- fraction of trades with pnl > 0
  avg_holding_days  DOUBLE PRECISION,
  primary_window    BOOLEAN NOT NULL DEFAULT TRUE,  -- consumer-read row; latest run per (strategy_id, primary_window=true)
  config_json       JSONB,                      -- params, max_hold_days, etc.
  notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy_run_at
  ON strategy_backtest_runs (strategy_id, run_at DESC);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_primary
  ON strategy_backtest_runs (strategy_id, run_at DESC)
  WHERE primary_window = TRUE;


CREATE TABLE IF NOT EXISTS strategy_backtest_regimes (
  run_id            UUID NOT NULL REFERENCES strategy_backtest_runs(run_id),
  regime_state      TEXT NOT NULL,              -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
  trade_count       INT NOT NULL,
  sharpe            DOUBLE PRECISION,           -- NULL when trade_count < 5 (not statistically meaningful)
  max_dd_pct        DOUBLE PRECISION,
  return_pct        DOUBLE PRECISION,           -- compounded across trades in this regime
  hit_rate          DOUBLE PRECISION,
  avg_pnl_pct       DOUBLE PRECISION,           -- mean per-trade pnl
  avg_holding_days  DOUBLE PRECISION,
  oos_days_in_regime INT,                       -- # trading days the regime was active during OOS
  PRIMARY KEY (run_id, regime_state)
);

CREATE INDEX IF NOT EXISTS idx_backtest_regimes_run
  ON strategy_backtest_regimes (run_id);


CREATE TABLE IF NOT EXISTS strategy_backtest_trades (
  run_id            UUID NOT NULL REFERENCES strategy_backtest_runs(run_id),
  trade_seq         INT NOT NULL,               -- 1..N ordering within the run
  strategy_id       TEXT NOT NULL,
  ticker            TEXT NOT NULL,
  direction         TEXT NOT NULL,              -- 'long' | 'short'
  entry_date        DATE NOT NULL,
  entry_price       DOUBLE PRECISION NOT NULL,
  exit_date         DATE,
  exit_price        DOUBLE PRECISION,
  exit_reason       TEXT,                        -- 'target' | 'stop' | 'max_hold' | 'end_of_data'
  pnl_pct           DOUBLE PRECISION,
  holding_days      INT,
  entry_regime      TEXT,                       -- regime live on entry_date
  signal_stop       DOUBLE PRECISION,
  signal_target     DOUBLE PRECISION,
  PRIMARY KEY (run_id, trade_seq)
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_strategy
  ON strategy_backtest_trades (strategy_id, entry_date DESC);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_regime
  ON strategy_backtest_trades (entry_regime);
