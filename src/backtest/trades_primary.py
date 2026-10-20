"""Writer-side maintenance of ``strategy_backtest_trades_primary`` (migration 166).

The hot table is a CACHE of ``strategy_backtest_trades`` rows whose run is the
strategy's primary window. It is kept in sync inside the SAME transaction that
demotes the prior primary run (unified_backtest persist), so it can never be
visible out of step with ``strategy_backtest_runs.primary_window``.

Rules:
  * Never commits - the caller owns the transaction.
  * The hot table is the ONLY DELETE target; the base table is never touched.
  * Presence is checked with ``to_regclass`` (never try/except: a failed
    statement aborts the caller's transaction). A missing table (migration
    166 rolled back) is a logged no-op.
"""
import os
import sys

HOT_TABLE = 'strategy_backtest_trades_primary'

TRADE_COLUMNS = (
    'run_id', 'trade_seq', 'strategy_id', 'ticker', 'direction',
    'entry_date', 'entry_price', 'exit_date', 'exit_price',
    'exit_reason', 'pnl_pct', 'holding_days', 'entry_regime',
    'signal_stop', 'signal_target',
)

_COLS_SQL = ', '.join(TRADE_COLUMNS)

LEGACY_TABLE = 'strategy_backtest_trades'


def trades_source() -> str:
    """Table the readers select backtest trades from. Read on EVERY call (never
    cached at import): the hot table only when OPENCLAW_BT_TRADES_HOT == '1'."""
    return HOT_TABLE if os.environ.get('OPENCLAW_BT_TRADES_HOT') == '1' else LEGACY_TABLE


def hot_table_exists(cur) -> bool:
    cur.execute("SELECT to_regclass('strategy_backtest_trades_primary') IS NOT NULL")
    row = cur.fetchone()
    return bool(row and row[0])


def sync_primary_trades(cur, strategy_id, run_id, trade_rows):
    """Replace the strategy's hot rows with the new primary run's trades."""
    if not hot_table_exists(cur):
        print(f'[bt_trades_primary] table absent (migration 166 not applied) '
              f'— skip strategy={strategy_id} run_id={run_id}')
        return None
    cur.execute(f'DELETE FROM {HOT_TABLE} WHERE strategy_id = %s', (strategy_id,))
    deleted = cur.rowcount
    inserted = 0
    if trade_rows:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, f"""
            INSERT INTO {HOT_TABLE}
              ({_COLS_SQL})
            VALUES %s
        """, trade_rows, page_size=500)
        inserted = len(trade_rows)
    print(f'[bt_trades_primary] strategy={strategy_id} run_id={run_id} '
          f'deleted={deleted} inserted={inserted}')
    return inserted


def resync_strategy(cur, strategy_id) -> int:
    """Rebuild one strategy's hot rows from the base table (primary runs only)."""
    cur.execute('LOCK TABLE strategy_backtest_runs IN SHARE MODE')
    cur.execute(f'DELETE FROM {HOT_TABLE} WHERE strategy_id = %s', (strategy_id,))
    tcols = ', '.join(f't.{c}' for c in TRADE_COLUMNS)
    cur.execute(f"""
        INSERT INTO {HOT_TABLE} ({_COLS_SQL})
        SELECT {tcols}
          FROM strategy_backtest_trades t
          JOIN strategy_backtest_runs r
            ON r.run_id = t.run_id AND r.primary_window
         WHERE r.strategy_id = %s
    """, (strategy_id,))
    return cur.rowcount


def sync_primary_trades_safe(cur, strategy_id, run_id, trade_rows):
    """sync_primary_trades inside a SAVEPOINT: a cache failure must never abort the
    caller's persist transaction (a ~900 s fleet run). On failure only the sync is
    rolled back; the strategy's previous hot rows stay until ``--resync`` repairs them."""
    cur.execute('SAVEPOINT trades_primary')
    try:
        n = sync_primary_trades(cur, strategy_id, run_id, trade_rows)
        cur.execute('RELEASE SAVEPOINT trades_primary')
        return n
    except Exception as e:
        cur.execute('ROLLBACK TO SAVEPOINT trades_primary')
        print(f'[bt_trades_primary] skipped: {type(e).__name__} '
              f'strategy={strategy_id} run_id={run_id}', file=sys.stderr, flush=True)
        return None


# ── drift detection (shared by the system check and the --resync CLI) ──────
_Q_COUNTS = f"""
    WITH p AS (SELECT run_id, strategy_id FROM strategy_backtest_runs WHERE primary_window),
         -- correlated count per primary run: forces index-only scans on the PK
         -- (run_id, trade_seq); the JOIN...GROUP BY form seq-scanned the 8 GB base
         -- table (28.7 s vs 8.4 s measured on production 2026-10-20).
         b AS (SELECT p.run_id,
                      (SELECT count(*) FROM {LEGACY_TABLE} t WHERE t.run_id = p.run_id) AS n
                 FROM p),
         h AS (SELECT run_id, count(*) AS n FROM {HOT_TABLE} GROUP BY run_id)
    SELECT p.run_id, p.strategy_id, coalesce(b.n, 0), coalesce(h.n, 0)
      FROM p LEFT JOIN b ON b.run_id = p.run_id LEFT JOIN h ON h.run_id = p.run_id
     WHERE coalesce(b.n, 0) <> coalesce(h.n, 0)
     ORDER BY p.strategy_id, p.run_id
"""
_Q_ORPHANS = f"""
    SELECT count(DISTINCT h.run_id) FROM {HOT_TABLE} h
      LEFT JOIN strategy_backtest_runs r ON r.run_id = h.run_id
     WHERE r.run_id IS NULL OR NOT r.primary_window
"""
_Q_IDMIS = f"""
    SELECT count(*) FROM (SELECT DISTINCT h.run_id, h.strategy_id FROM {HOT_TABLE} h
      JOIN strategy_backtest_runs r ON r.run_id = h.run_id
     WHERE h.strategy_id <> r.strategy_id) x
"""
_Q_NPRIMARY = 'SELECT count(*) FROM strategy_backtest_runs WHERE primary_window'


def drift_report(cur):
    """(count_mismatches, orphan_runs, id_mismatches, n_primary_runs); hot table must exist."""
    cur.execute(_Q_COUNTS)
    mism = [tuple(r) for r in cur.fetchall()]
    cur.execute(_Q_ORPHANS)
    orphans = int(cur.fetchone()[0])
    cur.execute(_Q_IDMIS)
    idm = int(cur.fetchone()[0])
    cur.execute(_Q_NPRIMARY)
    n = int(cur.fetchone()[0])
    return mism, orphans, idm, n


def _drifted_strategy_ids(cur) -> list:
    """Strategies needing a resync: count mismatches, plus those owning orphan /
    mis-attributed hot rows (the hot rows' own strategy_id is wrong or its run is
    no longer primary, so both the stale hot strategy and the run's strategy)."""
    sids = {m[1] for m in drift_report(cur)[0]}
    cur.execute(f"""
        SELECT DISTINCT h.strategy_id FROM {HOT_TABLE} h
          LEFT JOIN strategy_backtest_runs r ON r.run_id = h.run_id
         WHERE r.run_id IS NULL OR NOT r.primary_window OR h.strategy_id <> r.strategy_id
        UNION
        SELECT DISTINCT r.strategy_id FROM {HOT_TABLE} h
          JOIN strategy_backtest_runs r ON r.run_id = h.run_id
         WHERE h.strategy_id <> r.strategy_id""")
    sids.update(r[0] for r in cur.fetchall())
    return sorted(sids)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='backtest.trades_primary',
                                 description='Repair strategy_backtest_trades_primary')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--resync', metavar='STRATEGY_ID')
    g.add_argument('--resync-drifted', action='store_true')
    args = ap.parse_args(argv)
    import psycopg2
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL', '')
    if not uri:
        print('[bt_trades_primary] POSTGRES_URI not set', file=sys.stderr)
        return 2
    try:
        conn = psycopg2.connect(uri, connect_timeout=5)
    except Exception as e:
        print(f'[bt_trades_primary] connect failed: {type(e).__name__}: {e}',
              file=sys.stderr)
        return 2
    try:
        cur = conn.cursor()
        if not hot_table_exists(cur):
            print('[bt_trades_primary] table absent (migration 166 not applied)',
                  file=sys.stderr)
            conn.rollback()
            return 2
        sids = [args.resync] if args.resync else _drifted_strategy_ids(cur)
        conn.commit()   # end the read txn before per-strategy write txns
        failed = 0
        for sid in sids:
            try:
                n = resync_strategy(cur, sid)
                conn.commit()
            except Exception as e:
                conn.rollback()
                failed += 1
                print(f'[bt_trades_primary] resync FAILED strategy={sid}: '
                      f'{type(e).__name__}: {e}', file=sys.stderr)
                continue
            print(f'[bt_trades_primary] resync strategy={sid} inserted={n}')
        if not sids:
            print('[bt_trades_primary] nothing drifted')
        return 1 if failed else 0
    finally:
        conn.close()

if __name__ == '__main__':
    sys.exit(main())
