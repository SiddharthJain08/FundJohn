"""_record_dtbp_skip must write UNIQUE client_order_ids (2026-06-04 incident).

First live fill day: two DTBP skips in one run → both wrote coid='' → second
violated alpaca_submissions_client_order_id_key → alpaca step rc=1 → cycle
aborted with ~30 of 48 sized orders never attempted.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / 'src')):
    if p not in sys.path:
        sys.path.insert(0, p)

import psycopg2
import pytest

pytestmark = pytest.mark.integration


class _NoCommitConn:
    """Proxy: swallow commit() so the test transaction stays rollback-able."""
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def commit(self):
        pass


@pytest.fixture
def db_conn():
    uri = os.environ.get('POSTGRES_URI', 'postgresql://openclaw:password@localhost:5432/openclaw')
    conn = psycopg2.connect(uri)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _order(ticker):
    return {'ticker': ticker, 'side': 'sell', 'action': 'open_short',
            'pct_nav': 0.03, 'entry': 100.0, 'tif': 'day', 'order_class': 'simple'}


def test_two_dtbp_skips_do_not_collide(db_conn):
    from execution.alpaca_executor import _record_dtbp_skip

    conn = _NoCommitConn(db_conn)
    rd = date(2026, 6, 4)
    _record_dtbp_skip(conn, rd, _order('TESTAAA'), 100_000.0)
    _record_dtbp_skip(conn, rd, _order('TESTBBB'), 100_000.0)  # crashed pre-fix

    cur = db_conn.cursor()
    cur.execute("""SELECT ticker, client_order_id FROM alpaca_submissions
                   WHERE ticker IN ('TESTAAA','TESTBBB') AND run_date=%s""", (rd,))
    rows = dict(cur.fetchall())
    assert set(rows) == {'TESTAAA', 'TESTBBB'}, 'both audit rows must persist'
    coids = list(rows.values())
    assert all(c for c in coids), f'coids must be non-empty: {coids}'
    assert len(set(coids)) == 2, f'coids must be unique: {coids}'
    assert all(c.startswith('skip-dtbp-') for c in coids), 'audit coids self-describe'
