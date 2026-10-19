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
