"""test_sp6_b0_fill_capture.py — SP-6 Phase B0 per-order execution ledger.

finalize_execution_ledger reads filled ENTRY orders from alpaca_submissions on run_date
and writes official_close (the close[T+1] benchmark) + exec_ledger_usd
  = (official_close - filled_avg_price) x (direction_sign x filled_qty).
exec_ledger_usd > 0 ⟺ the fill beat the close (long below close / short above close).

DB tests use rollback isolation (no persistent side-effects). No execution_signals rows,
no broker injection — just alpaca_submissions rows + a closes dict.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.parity_mark import finalize_execution_ledger


@pytest.fixture
def db_conn():
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(os.environ['POSTGRES_URI'],
                            cursor_factory=psycopg2.extras.DictCursor)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


def _insert_submission(cur, *, run_date, ticker, strategy_id, direction,
                       qty, filled_qty, filled_avg_price, broker_status,
                       entry_price=100.0):
    """Insert one alpaca_submissions row carrying a (reconciled) broker fill.
    filled_avg_price=None / broker_status=None models an unreconciled order."""
    coid = f"b0t-{strategy_id}-{ticker}".replace('/', '-')
    cur.execute("""
        INSERT INTO alpaca_submissions
            (run_date, ticker, strategy_id, direction, qty, entry_price,
             time_in_force, order_class, client_order_id,
             broker_status, filled_qty, filled_avg_price, reconciled_at)
        VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s, NOW())
    """, (run_date, ticker, strategy_id, direction, qty, entry_price,
          'day', 'simple', coid, broker_status, filled_qty, filled_avg_price))


def _fetch(cur, *, run_date, ticker, strategy_id):
    cur.execute("""SELECT official_close, exec_ledger_usd
                     FROM alpaca_submissions
                    WHERE run_date=%s AND ticker=%s AND strategy_id=%s""",
                (run_date, ticker, strategy_id))
    return cur.fetchone()


@pytest.mark.integration
def test_long_filled_below_close_positive_ledger(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0LONG', 'ZZB0_STRAT_A'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=100,
                       filled_avg_price=99.0, broker_status='filled')

    n = finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    assert n == 1

    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    assert abs(float(r['official_close']) - 103.5) < 1e-6
    # (103.5 - 99.0) * (+1 * 100) = 450.0  → beat the close
    assert abs(float(r['exec_ledger_usd']) - 450.0) < 1e-6
