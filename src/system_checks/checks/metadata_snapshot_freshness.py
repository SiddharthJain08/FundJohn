"""Strategies-tagged check: ticker_metadata_snapshots written within 2 calendar days."""
from __future__ import annotations

import os
from datetime import date

import psycopg2

from ..registry import check
from ..types import Status


@check(name='metadata_snapshot_freshness', tags=['strategies'], requires=['db'])
def _metadata_snapshot_freshness():
    """Verify ticker_metadata_snapshots was written recently.
    PASS ≤1d, WARN ≤2d, FAIL >2d or table empty."""
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return Status.FAIL, 'POSTGRES_URI not set'
    try:
        with psycopg2.connect(uri) as c, c.cursor() as cur:
            cur.execute('SELECT MAX(snapshot_date) FROM ticker_metadata_snapshots')
            last = cur.fetchone()[0]
    except Exception as exc:
        return Status.FAIL, f'DB query failed: {exc}'
    if last is None:
        return Status.FAIL, 'no snapshots in table'
    age = (date.today() - last).days
    if age > 2:
        return Status.FAIL, f'stale {age}d'
    if age > 1:
        return Status.WARN, f'last={last} ({age}d ago)'
    return Status.PASS, f'last={last} ({age}d ago)'
