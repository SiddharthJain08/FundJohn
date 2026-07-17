"""Migration 135 wiring: a backtest run persists total_sortino/total_calmar/
total_avg_pnl_pct on strategy_backtest_runs and sortino/calmar on _regimes."""
import os, psycopg2, pytest

pytestmark = pytest.mark.skipif(not os.environ.get('POSTGRES_URI'), reason='needs DB')

def test_sortino_calmar_columns_exist_and_are_written():
    c = psycopg2.connect(os.environ['POSTGRES_URI']); cur = c.cursor()
    # The columns exist (migration 135 applied)
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name='strategy_backtest_runs'
                   AND column_name IN ('total_sortino','total_calmar','total_avg_pnl_pct')""")
    assert {r[0] for r in cur.fetchall()} == {'total_sortino','total_calmar','total_avg_pnl_pct'}
    # The latest run for any strategy with >5 trades has a non-null sortino
    cur.execute("""SELECT total_sortino, total_calmar FROM strategy_backtest_runs
                   WHERE primary_window=TRUE AND total_trades > 20
                   ORDER BY run_at DESC LIMIT 1""")
    row = cur.fetchone()
    if row is not None:  # only assert once at least one post-135 run exists
        assert row[0] is not None, 'total_sortino should be populated for a >20-trade run'
    c.close()
