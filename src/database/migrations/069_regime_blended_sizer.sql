-- 069_regime_blended_sizer.sql
-- Schema for regime-blended position sizing (spec 2026-05-11).

-- Per-regime sizing parameters; tunable without code changes.
CREATE TABLE IF NOT EXISTS regime_sizer_params (
  regime_state TEXT PRIMARY KEY,
  liquidity_param REAL NOT NULL CHECK (liquidity_param BETWEEN 0 AND 1),
  min_signal_notional_usd REAL NOT NULL CHECK (min_signal_notional_usd > 0),
  position_circuit_breaker_pct REAL NOT NULL CHECK (position_circuit_breaker_pct > 0),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO regime_sizer_params (regime_state, liquidity_param, min_signal_notional_usd, position_circuit_breaker_pct)
VALUES
  ('LOW_VOL',       1.00, 100, 0.020),
  ('TRANSITIONING', 0.75, 100, 0.015),
  ('HIGH_VOL',      0.50, 200, 0.010),
  ('CRISIS',        0.25, 500, 0.005)
ON CONFLICT (regime_state) DO NOTHING;

-- Per-strategy attribution of consolidated positions.
CREATE TABLE IF NOT EXISTS consolidation_contributions (
  consolidated_signal_id BIGINT NOT NULL,
  contributing_signal_id BIGINT NOT NULL,
  strategy_id TEXT NOT NULL,
  signal_position_size_usd REAL NOT NULL,
  attribution_weight REAL NOT NULL CHECK (attribution_weight BETWEEN 0 AND 1),
  contributed_direction INT NOT NULL CHECK (contributed_direction IN (-1, 1)),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (consolidated_signal_id, contributing_signal_id)
);
CREATE INDEX IF NOT EXISTS idx_consolidation_contrib_strategy
  ON consolidation_contributions (strategy_id, created_at DESC);

-- Cadence-skip audit (forensic + dashboard).
CREATE TABLE IF NOT EXISTS cadence_skips (
  id BIGSERIAL PRIMARY KEY,
  signal_date DATE NOT NULL,
  strategy_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  reason TEXT NOT NULL,
  context_json JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cadence_skips_date ON cadence_skips (signal_date DESC);

-- DRY-RUN parity output (30-day comparison).
CREATE TABLE IF NOT EXISTS parity_orders (
  id BIGSERIAL PRIMARY KEY,
  signal_date DATE NOT NULL,
  ticker TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('regime_blended', 'deterministic')),
  qty REAL NOT NULL,
  notional_usd REAL NOT NULL,
  bracket_json JSONB NOT NULL,
  contributing_signal_ids BIGINT[],
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_parity_orders_date_ticker
  ON parity_orders (signal_date DESC, ticker);

-- Intraday circuit-breaker audit.
CREATE TABLE IF NOT EXISTS circuit_breaker_fires (
  id BIGSERIAL PRIMARY KEY,
  ts_utc TIMESTAMPTZ NOT NULL,
  ticker TEXT NOT NULL,
  unrealized_pnl_pct_nav REAL NOT NULL,
  threshold_pct REAL NOT NULL,
  position_qty REAL NOT NULL,
  close_result_json JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Regime-eligibility analyzer thresholds.
CREATE TABLE IF NOT EXISTS regime_eligibility_thresholds (
  threshold_name TEXT PRIMARY KEY,
  value REAL NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO regime_eligibility_thresholds (threshold_name, value) VALUES
  ('min_sharpe',       0.5),
  ('min_trade_count', 20.0),
  ('min_avg_r',        0.0)
ON CONFLICT (threshold_name) DO NOTHING;

-- Per-strategy cadence state.
CREATE TABLE IF NOT EXISTS strategy_state (
  strategy_id TEXT PRIMARY KEY,
  last_fire_date DATE,
  next_fire_date DATE,
  avg_holding_days REAL,
  source TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Per-strategy attribution carrier on signal_pnl (for consolidated trades).
ALTER TABLE signal_pnl ADD COLUMN IF NOT EXISTS attribution_weight REAL DEFAULT 1.0;

-- Tradejohn DRY-RUN decision capture (Phase 2).
CREATE TABLE IF NOT EXISTS tradejohn_decisions_dryrun (
  id BIGSERIAL PRIMARY KEY,
  signal_date DATE NOT NULL,
  ticker TEXT NOT NULL,
  preliminary_size_usd REAL NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('approve', 'veto', 'scale')),
  multiplier REAL NOT NULL,
  rationale TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- One-time backfill: initialize strategy_state.last_fire_date from execution_signals.
INSERT INTO strategy_state (strategy_id, last_fire_date, source, updated_at)
SELECT DISTINCT strategy_id, MAX(signal_date), 'phase0_backfill', NOW()
  FROM execution_signals
 GROUP BY strategy_id
    ON CONFLICT (strategy_id) DO NOTHING;
