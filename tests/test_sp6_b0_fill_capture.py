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


@pytest.mark.integration
def test_short_filled_above_close_positive_ledger(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0SHORT', 'ZZB0_STRAT_B'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='short', qty=100, filled_qty=100,
                       filled_avg_price=105.0, broker_status='filled')
    finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    # (103.5 - 105.0) * (-1 * 100) = 150.0  → sold above the close, beat it
    assert abs(float(r['exec_ledger_usd']) - 150.0) < 1e-6


@pytest.mark.integration
def test_long_filled_above_close_negative_ledger(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0LONGNEG', 'ZZB0_STRAT_C'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=100,
                       filled_avg_price=105.0, broker_status='filled')
    finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    # (103.5 - 105.0) * (+1 * 100) = -150.0  → paid up vs the close
    assert abs(float(r['exec_ledger_usd']) + 150.0) < 1e-6


@pytest.mark.integration
def test_unreconciled_fill_left_null(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0NULL', 'ZZB0_STRAT_D'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=None,
                       filled_avg_price=None, broker_status=None)
    n = finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    assert n == 0
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    assert r['official_close'] is None
    assert r['exec_ledger_usd'] is None


@pytest.mark.integration
def test_partial_fill_uses_filled_qty(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0PARTIAL', 'ZZB0_STRAT_E'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=60,
                       filled_avg_price=99.0, broker_status='partial')
    finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    # (103.5 - 99.0) * (1 * 60) = 270.0
    assert abs(float(r['exec_ledger_usd']) - 270.0) < 1e-6


@pytest.mark.integration
def test_crypto_symbol_normalization(db_conn):
    """Submission ticker BTC/USD, closes keyed BTC-USD — normalization (/ → -) must
    match; fractional qty preserved."""
    cur = db_conn.cursor()
    run_date = date.today()
    strat = 'ZZB0_STRAT_F'
    _insert_submission(cur, run_date=run_date, ticker='BTC/USD', strategy_id=strat,
                       direction='long', qty=1, filled_qty=0.5,
                       filled_avg_price=49500.0, broker_status='filled',
                       entry_price=49000.0)
    finalize_execution_ledger(cur, {'BTC-USD': 50000.0}, run_date)
    r = _fetch(cur, run_date=run_date, ticker='BTC/USD', strategy_id=strat)
    # (50000 - 49500) * (1 * 0.5) = 250.0
    assert abs(float(r['exec_ledger_usd']) - 250.0) < 1e-6


@pytest.mark.integration
def test_ticker_absent_from_closes_left_null(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0ABSENT', 'ZZB0_STRAT_G'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=100,
                       filled_avg_price=99.0, broker_status='filled')
    n = finalize_execution_ledger(cur, {'SOMETHINGELSE': 103.5}, run_date)
    assert n == 0
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    assert r['exec_ledger_usd'] is None


@pytest.mark.integration
def test_orphan_close_order_excluded(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0ORPHAN', '__close_orphan__'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=100,
                       filled_avg_price=99.0, broker_status='filled')
    n = finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    assert n == 0
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    assert r['official_close'] is None
    assert r['exec_ledger_usd'] is None


@pytest.mark.integration
def test_idempotent_rerun_identical(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0IDEM', 'ZZB0_STRAT_H'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=100,
                       filled_avg_price=99.0, broker_status='filled')
    finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    first = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    second = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    assert float(second['exec_ledger_usd']) == float(first['exec_ledger_usd'])
    assert float(second['official_close']) == float(first['official_close'])


@pytest.mark.integration
def test_empty_closes_returns_zero(db_conn):
    assert finalize_execution_ledger(db_conn.cursor(), {}, date.today()) == 0
