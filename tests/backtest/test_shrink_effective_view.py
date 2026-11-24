"""Read-only live-DB check of universe_shrink_metrics_effective (migration 170,
spec 2026-11-24 shrink Sharpe marks fix, Part A).

For S_beta_spy's latest primary run: every chosen non-TOTAL row whose
trade_count equals the run's own strategy_backtest_regimes row must read
from_run_row = TRUE and carry that regimes row's sharpe. Skips when the DB,
the view, or the rows are unavailable. Never writes.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip('psycopg2')
import psycopg2.extras  # noqa: E402

ENV_PATH = Path('/root/openclaw/.env')
STRATEGY = 'S_beta_spy'


def _postgres_uri():
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith('POSTGRES_URI='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return None


@pytest.fixture
def cur():
    uri = _postgres_uri()
    if not uri:
        pytest.skip('POSTGRES_URI unavailable')
    try:
        conn = psycopg2.connect(uri, connect_timeout=5)
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f'DB unreachable: {type(e).__name__}')
    conn.set_session(readonly=True)
    try:
        yield conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    finally:
        conn.rollback()
        conn.close()


def test_equal_count_chosen_rows_read_the_run_row(cur):
    cur.execute("SELECT to_regclass('universe_shrink_metrics_effective') AS v")
    if cur.fetchone()['v'] is None:
        pytest.skip('migration 170 not applied')
    cur.execute("""
        SELECT run_id FROM strategy_backtest_runs
         WHERE strategy_id = %s AND primary_window = TRUE
         ORDER BY run_at DESC LIMIT 1
    """, (STRATEGY,))
    run = cur.fetchone()
    if not run:
        pytest.skip(f'no primary run for {STRATEGY}')
    cur.execute("""
        SELECT v.regime_state, v.trade_count, v.sharpe, v.from_run_row,
               br.trade_count AS run_n, br.sharpe AS run_sharpe
          FROM universe_shrink_metrics_effective v
          JOIN strategy_backtest_regimes br
            ON br.run_id = v.run_id AND br.regime_state = v.regime_state
         WHERE v.run_id = %s AND v.chosen AND v.regime_state <> 'TOTAL'
           AND br.trade_count = v.trade_count
    """, (run['run_id'],))
    rows = cur.fetchall()
    if not rows:
        pytest.skip(f'no equal-count chosen rows for {STRATEGY}')
    for r in rows:
        assert r['from_run_row'] is True, r['regime_state']
        if r['run_sharpe'] is None:
            assert r['sharpe'] is None, r['regime_state']
        else:
            assert math.isclose(float(r['sharpe']), float(r['run_sharpe']),
                                rel_tol=0, abs_tol=1e-12), r['regime_state']
