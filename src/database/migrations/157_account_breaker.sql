-- 157: account-level risk breaker (spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md
-- §3 C1, operator ruling R2). Two tables, both additive; nothing here deletes
-- or rewrites. Stream B reserved 155 and 156.
--
-- account_breaker_state is a SINGLETON (id = 1): the breaker is an account-wide
-- latch, not a per-ticker fact. History of individual closes already lives in
-- circuit_breaker_fires, which the account breaker writes to as well so
-- open_reconcile.reconcile_broker_closes and the sizer's risk-exit cooldown
-- (_load_recent_risk_exits) pick its flattens up with no new plumbing.
CREATE TABLE IF NOT EXISTS account_breaker_state (
  id               INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  halted           BOOLEAN NOT NULL DEFAULT FALSE,
  reason           TEXT,                 -- 'drawdown' | 'daily_loss' | 'drawdown+daily_loss'
  breached_at      TIMESTAMPTZ,          -- the re-arm token the operator must echo back
  peak_alpha_nav   DOUBLE PRECISION,     -- rolling peak of equity - benchmark market value
  dd               DOUBLE PRECISION,     -- alpha_nav / peak_alpha_nav - 1 at the last tick
  daily            DOUBLE PRECISION,     -- equity / opening_equity - 1 at the last tick
  pending_flatten  BOOLEAN NOT NULL DEFAULT FALSE,
  rearmed_at       TIMESTAMPTZ,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO account_breaker_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE account_breaker_state IS
  'singleton account-breaker latch, spec 2026-09-12 C1; re-arm is operator-only via OPENCLAW_ACCOUNT_BREAKER_REARM=<breached_at iso>';

-- Opening equity per NYSE session. `estimated` is TRUE when the value was
-- reconstructed from the current equity because neither a stored row nor a
-- logs/pnl_daily_ohlc.json candle for the session was available.
CREATE TABLE IF NOT EXISTS account_daily_open (
  session_date    DATE PRIMARY KEY,
  opening_equity  DOUBLE PRECISION NOT NULL,
  estimated       BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE account_daily_open IS
  'per-session opening equity for the C1 daily-loss rule; estimated=true when reconstructed';
