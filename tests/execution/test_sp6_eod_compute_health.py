#!/usr/bin/env python3
"""
Test: eod_compute_health sentinel row creation.

Run: cd /root/openclaw && python3 -m pytest tests/test_sp6_eod_compute_health.py -v
"""

from __future__ import annotations

import sys
import os
import json
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest
import psycopg2
import psycopg2.extras


pytestmark = pytest.mark.integration


@pytest.fixture
def db_conn():
    """Connect to test DB with auto-rollback teardown (DictCursor)."""
    uri = os.environ.get('POSTGRES_URI', 'postgresql://openclaw:password@localhost:5432/openclaw')
    conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.DictCursor)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_write_eod_health_healthy_true_on_good_run(db_conn):
    """When rc=0, regime_ok=True, universe_size>0, n_strategies_ok>0, healthy should be True."""
    from execution.engine import write_eod_health

    cur = db_conn.cursor()
    run_date = date(2026, 5, 31)

    write_eod_health(
        cur,
        run_date,
        rc=0,
        n_strategies_ok=5,
        n_strategies_total=10,
        regime_ok=True,
        universe_size=100,
    )

    cur.execute(
        "SELECT * FROM eod_compute_health WHERE run_date = %s ORDER BY run_at DESC LIMIT 1",
        (run_date,),
    )
    row = cur.fetchone()

    assert row is not None, "No eod_compute_health row inserted"
    assert row['rc'] == 0
    assert row['n_strategies_ok'] == 5
    assert row['n_strategies_total'] == 10
    assert row['regime_ok'] is True
    assert row['universe_size'] == 100
    assert row['healthy'] is True, "healthy should be True when all conditions pass"
    assert isinstance(row['detail'], dict)


def test_write_eod_health_healthy_false_on_zero_universe(db_conn):
    """When universe_size=0, healthy should be False even if other conditions pass."""
    from execution.engine import write_eod_health

    cur = db_conn.cursor()
    run_date = date(2026, 5, 31)

    write_eod_health(
        cur,
        run_date,
        rc=0,
        n_strategies_ok=5,
        n_strategies_total=10,
        regime_ok=True,
        universe_size=0,
    )

    cur.execute(
        "SELECT * FROM eod_compute_health WHERE run_date = %s AND universe_size = 0 LIMIT 1",
        (run_date,),
    )
    row = cur.fetchone()

    assert row is not None
    assert row['healthy'] is False, "healthy should be False when universe_size=0"


def test_write_eod_health_healthy_false_on_nonzero_rc(db_conn):
    """When rc != 0, healthy should be False."""
    from execution.engine import write_eod_health

    cur = db_conn.cursor()
    run_date = date(2026, 5, 31)

    write_eod_health(
        cur,
        run_date,
        rc=1,
        n_strategies_ok=5,
        n_strategies_total=10,
        regime_ok=True,
        universe_size=100,
    )

    cur.execute(
        "SELECT * FROM eod_compute_health WHERE run_date = %s AND rc = 1 LIMIT 1",
        (run_date,),
    )
    row = cur.fetchone()

    assert row is not None
    assert row['healthy'] is False, "healthy should be False when rc != 0"


def test_write_eod_health_healthy_false_on_regime_not_ok(db_conn):
    """When regime_ok=False, healthy should be False."""
    from execution.engine import write_eod_health

    cur = db_conn.cursor()
    run_date = date(2026, 5, 31)

    write_eod_health(
        cur,
        run_date,
        rc=0,
        n_strategies_ok=5,
        n_strategies_total=10,
        regime_ok=False,
        universe_size=100,
    )

    cur.execute(
        "SELECT * FROM eod_compute_health WHERE run_date = %s AND regime_ok = false LIMIT 1",
        (run_date,),
    )
    row = cur.fetchone()

    assert row is not None
    assert row['healthy'] is False, "healthy should be False when regime_ok=False"


def test_write_eod_health_healthy_false_on_zero_strategies_ok(db_conn):
    """When n_strategies_ok=0, healthy should be False."""
    from execution.engine import write_eod_health

    cur = db_conn.cursor()
    run_date = date(2026, 5, 31)

    write_eod_health(
        cur,
        run_date,
        rc=0,
        n_strategies_ok=0,
        n_strategies_total=10,
        regime_ok=True,
        universe_size=100,
    )

    cur.execute(
        "SELECT * FROM eod_compute_health WHERE run_date = %s AND n_strategies_ok = 0 LIMIT 1",
        (run_date,),
    )
    row = cur.fetchone()

    assert row is not None
    assert row['healthy'] is False, "healthy should be False when n_strategies_ok=0"


def test_write_eod_health_detail_json_serialized(db_conn):
    """detail column should contain JSON with all run metadata."""
    from execution.engine import write_eod_health

    cur = db_conn.cursor()
    run_date = date(2026, 5, 31)

    write_eod_health(
        cur,
        run_date,
        rc=0,
        n_strategies_ok=3,
        n_strategies_total=8,
        regime_ok=True,
        universe_size=50,
    )

    cur.execute(
        "SELECT detail FROM eod_compute_health WHERE run_date = %s AND n_strategies_ok = 3 LIMIT 1",
        (run_date,),
    )
    row = cur.fetchone()

    assert row is not None
    detail = row['detail']
    assert isinstance(detail, dict)
    assert detail['rc'] == 0
    assert detail['n_strategies_ok'] == 3
    assert detail['n_strategies_total'] == 8
    assert detail['regime_ok'] is True
    assert detail['universe_size'] == 50
    assert detail['healthy'] is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
