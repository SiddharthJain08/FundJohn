#!/usr/bin/env python3
"""
Fix C (2026-06-04): eod_compute_health.panel_max_date + freshness-aware healthy.

The 06-02/06-03 EOD computes ran on close[T−1] with healthy=True — nothing
recorded what data the engine actually decided on. panel_max_date records the
pre-proxy parquet panel max date; when the run is a post-close compute
(panel_fresh_required=True) a stale panel flips healthy to False so the 9:28
flatten gate + prefire watchdog see it.

Run: cd /root/openclaw && python3 -m pytest tests/test_eod_health_panel_max_date.py -v
"""

from __future__ import annotations

import sys
import os
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
    uri = os.environ.get('POSTGRES_URI', 'postgresql://openclaw:password@localhost:5432/openclaw')
    conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.DictCursor)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _latest(cur, run_date):
    cur.execute(
        "SELECT * FROM eod_compute_health WHERE run_date = %s ORDER BY run_at DESC LIMIT 1",
        (run_date,),
    )
    return cur.fetchone()


GOOD = dict(rc=0, n_strategies_ok=5, n_strategies_total=10, regime_ok=True, universe_size=100)


def test_fresh_panel_required_healthy(db_conn):
    from execution.engine import write_eod_health
    cur = db_conn.cursor()
    rd = date(2026, 6, 4)
    write_eod_health(cur, rd, **GOOD, panel_max_date=rd, panel_fresh_required=True)
    row = _latest(cur, rd)
    assert row['healthy'] is True
    assert row['panel_max_date'] == rd


def test_stale_panel_required_unhealthy(db_conn):
    """Post-close compute on a close[T−1] panel must NOT report healthy."""
    from execution.engine import write_eod_health
    cur = db_conn.cursor()
    rd = date(2026, 6, 4)
    write_eod_health(cur, rd, **GOOD, panel_max_date=date(2026, 6, 3), panel_fresh_required=True)
    row = _latest(cur, rd)
    assert row['healthy'] is False
    assert row['panel_max_date'] == date(2026, 6, 3)


def test_stale_panel_not_required_stays_healthy(db_conn):
    """Intraday redeploy engine runs use close[T−1] CORRECTLY — exempt."""
    from execution.engine import write_eod_health
    cur = db_conn.cursor()
    rd = date(2026, 6, 4)
    write_eod_health(cur, rd, **GOOD, panel_max_date=date(2026, 6, 3), panel_fresh_required=False)
    row = _latest(cur, rd)
    assert row['healthy'] is True


def test_legacy_call_shape_unchanged(db_conn):
    """Existing callers without panel kwargs keep byte-identical behavior."""
    from execution.engine import write_eod_health
    cur = db_conn.cursor()
    rd = date(2026, 6, 4)
    write_eod_health(cur, rd, **GOOD)
    row = _latest(cur, rd)
    assert row['healthy'] is True
    assert row['panel_max_date'] is None


def test_missing_panel_date_when_required_unhealthy(db_conn):
    """Required but panel date unknown (load failed) → fail-closed."""
    from execution.engine import write_eod_health
    cur = db_conn.cursor()
    rd = date(2026, 6, 4)
    write_eod_health(cur, rd, **GOOD, panel_max_date=None, panel_fresh_required=True)
    row = _latest(cur, rd)
    assert row['healthy'] is False
