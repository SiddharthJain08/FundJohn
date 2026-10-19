-- 166: strategy_backtest_trades_primary — hot cache of the CURRENT primary runs' trades.
--
-- This table is a CACHE (R3): a derived copy of strategy_backtest_trades rows whose run
-- is the primary window. It is rebuildable and is not in the append-only list; the base
-- table strategy_backtest_trades is never deleted from or updated by this file.
--
-- Runner semantics: postgres.js migrate() sends this file as ONE implicit transaction on
-- every johnbot start. So: no CONCURRENTLY, no COMMIT between chunks. Every statement is
-- idempotent, and the backfill is skipped once the table comment says 'backfilled:166'.
--
-- Expected first-run cost: ~5.1 M rows / 304 runs, 2-6 min on this box. Reads ~0.85 GB of
-- contiguous heap and writes ~0.8 GB heap + ~0.37 GB PK + ~0.05 GB secondary index, so
-- disk grows by ~1.3 GB. ~1.5-2 GB of transient WAL is recycled by checkpoints, because
-- an open transaction does not retain WAL. Backtest persists block for that duration
-- (SHARE lock on strategy_backtest_runs).
-- Later starts cost one catalog lookup.
-- The controller pre-applies it off-hours with `psql -1 -f` (see Runbook).

CREATE TABLE IF NOT EXISTS strategy_backtest_trades_primary (
  run_id            UUID NOT NULL,
  trade_seq         INT NOT NULL,
  strategy_id       TEXT NOT NULL,
  ticker            TEXT NOT NULL,
  direction         TEXT NOT NULL,
  entry_date        DATE NOT NULL,
  entry_price       DOUBLE PRECISION NOT NULL,
  exit_date         DATE,
  exit_price        DOUBLE PRECISION,
  exit_reason       TEXT,
  pnl_pct           DOUBLE PRECISION,
  holding_days      INT,
  entry_regime      TEXT,
  signal_stop       DOUBLE PRECISION,
  signal_target     DOUBLE PRECISION,
  PRIMARY KEY (run_id, trade_seq)
);

DO $$
DECLARE
  rid     UUID;
  n_runs  BIGINT := 0;
  n_rows  BIGINT := 0;
  n_chunk BIGINT;
BEGIN
  IF obj_description('strategy_backtest_trades_primary'::regclass, 'pg_class') = 'backfilled:166' THEN
    RAISE NOTICE '[166] backfill already done — skipped';
    RETURN;
  END IF;

  PERFORM set_config('lock_timeout', '60s', true);
  PERFORM set_config('statement_timeout', '0', true);
  -- Blocks a concurrent persist or rename from committing mid-backfill, so the cache
  -- cannot drift on day one.
  LOCK TABLE strategy_backtest_runs IN SHARE MODE;

  FOR rid IN SELECT run_id FROM strategy_backtest_runs WHERE primary_window ORDER BY run_id LOOP
    INSERT INTO strategy_backtest_trades_primary
      (run_id, trade_seq, strategy_id, ticker, direction, entry_date, entry_price,
       exit_date, exit_price, exit_reason, pnl_pct, holding_days, entry_regime,
       signal_stop, signal_target)
    SELECT run_id, trade_seq, strategy_id, ticker, direction, entry_date, entry_price,
           exit_date, exit_price, exit_reason, pnl_pct, holding_days, entry_regime,
           signal_stop, signal_target
      FROM strategy_backtest_trades
     WHERE run_id = rid
    ON CONFLICT (run_id, trade_seq) DO NOTHING;
    GET DIAGNOSTICS n_chunk = ROW_COUNT;
    n_rows := n_rows + n_chunk;
    n_runs := n_runs + 1;
  END LOOP;

  RAISE NOTICE '[166] backfilled % rows from % primary runs', n_rows, n_runs;
  COMMENT ON TABLE strategy_backtest_trades_primary IS 'backfilled:166';
END
$$;

-- AFTER the backfill (bulk build).
CREATE INDEX IF NOT EXISTS idx_bt_trades_primary_strategy
  ON strategy_backtest_trades_primary (strategy_id, entry_date);
