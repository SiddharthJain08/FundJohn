-- 171: universe_shrink_parity (2026-11-25 follow-up to mig 170 / the shrink Sharpe marks fix).
-- run_universe_shrink.py checks that the marks-based full-tier Sharpe reproduces the
-- backtest of record (OPENCLAW_SHRINK_PARITY_TOL). A failure wrote NO rows — but the
-- strategy's older rows for that run stayed `chosen`, so readers kept consuming the
-- stale smear numbers (22 long/short books on 2026-11-24, un-chosen by hand). The
-- verdict is now persisted here and a failure un-chooses the run's rows in the writer,
-- so every reader falls back to strategy_backtest_regimes on its own.
CREATE TABLE IF NOT EXISTS universe_shrink_parity (
  strategy_id  text        NOT NULL,
  run_id       text        NOT NULL,
  parity_ok    boolean     NOT NULL,
  full_sharpe  double precision,
  run_sharpe   double precision,
  tol          double precision,
  checked_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (strategy_id, run_id)
);
