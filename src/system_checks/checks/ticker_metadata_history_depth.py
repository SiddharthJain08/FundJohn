"""Strategies-tagged check: ticker_metadata_snapshots has ≥ 5y depth.

Reads min(snapshot_date) and confirms it reaches back to at least 2021-06-01
(per SP-2 Phase B target of 5y point-in-time universe coverage).

Advisory only — WARN, never FAIL — because the daily cycle operates on the
live universe path; absence of backfill depth limits per-bar historical
backtests but does not block live trading.
"""
from __future__ import annotations

import os
from datetime import date

import psycopg2

from ..registry import check
from ..types import Status


# Five-year depth target: anything from 2021-06-01 or earlier qualifies.
_DEPTH_TARGET = date(2021, 6, 1)


@check(name='ticker_metadata_history_depth', tags=['strategies'], requires=['db'])
def _ticker_metadata_history_depth():
    """Verify ticker_metadata_snapshots reaches back to 2021-06-01 or earlier.
    PASS if min(snapshot_date) ≤ 2021-06-01 (≥5y depth),
    WARN if shallower or no snapshots present."""
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL', '')
    if not uri:
        return Status.FAIL, 'POSTGRES_URI not set'
    try:
        with psycopg2.connect(uri) as c, c.cursor() as cur:
            cur.execute('SELECT MIN(snapshot_date) FROM ticker_metadata_snapshots')
            earliest = cur.fetchone()[0]
    except Exception as exc:
        return Status.FAIL, f'DB query failed: {exc}'
    if earliest is None:
        return Status.WARN, 'no snapshots in table'
    if earliest <= _DEPTH_TARGET:
        return Status.PASS, f'earliest={earliest} (≥5y depth)'
    return Status.WARN, f'earliest={earliest} (< {_DEPTH_TARGET}, shallow)'
