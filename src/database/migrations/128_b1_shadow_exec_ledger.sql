-- 128_b1_shadow_exec_ledger.sql
-- SP-6 Phase B1 shadow execution ledger. Observation-only; never affects trading.
CREATE TABLE IF NOT EXISTS b1_shadow_exec_ledger (
  id              BIGSERIAL PRIMARY KEY,
  mode            TEXT NOT NULL,             -- 'case_study' | 'live_shadow'
  run_date        DATE,                      -- the T+1 session worked
  ticker          TEXT NOT NULL,
  strategy_id     TEXT,
  signal_id       UUID,
  regime_state    TEXT,
  signed_qty      NUMERIC,
  s_i             NUMERIC,
  lam             NUMERIC,
  actual_fill     NUMERIC,                   -- qty-weighted sim fill (alpha plan)
  close_t1        NUMERIC,
  exec_ledger     NUMERIC,                   -- (close_t1 - actual_fill)*signed_qty (alpha plan)
  naive_ledger    NUMERIC,                   -- vs the naive 3:55 fill
  vwap_base_ledger NUMERIC,                  -- vs the lam=0 VWAP-base plan
  completion      NUMERIC,                   -- filled_qty / signed_qty
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_b1_shadow_run_date ON b1_shadow_exec_ledger (run_date);
CREATE INDEX IF NOT EXISTS ix_b1_shadow_mode ON b1_shadow_exec_ledger (mode);
