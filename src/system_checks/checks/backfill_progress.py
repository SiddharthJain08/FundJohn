"""Storage+strategies check: SP-2 Phase B backfill progress monitoring.

FAIL if any chunk has been in 'in_progress' state for > 24h (stuck), since the
backfill driver is supposed to advance every chunk to a terminal state
(validated/promoted/quarantined/failed) on a much shorter loop. WARN if any
quarantined rows exist at all (data quality flag worth investigating). PASS
otherwise.

Distinct from the doctor.py preflight peer (`backfill_progress`, slow=True),
which gates on a higher quarantined threshold (>100 → FAIL) so a single bad
chunk doesn't block the daily cycle.
"""
from __future__ import annotations

import os

import psycopg2

from ..registry import check
from ..types import Status


@check(name='backfill_progress', tags=['storage', 'strategies'], requires=['db'])
def _backfill_progress():
    """Surface stuck chunks (in_progress > 24h → FAIL) and quarantined rows
    (any → WARN) in backfill_audit."""
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL', '')
    if not uri:
        return Status.FAIL, 'POSTGRES_URI not set'
    try:
        with psycopg2.connect(uri) as c, c.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM backfill_audit "
                "WHERE status='in_progress' "
                "AND started_at < NOW() - INTERVAL '24 hours'"
            )
            stuck = int(cur.fetchone()[0])
            cur.execute(
                "SELECT count(*) FROM backfill_audit WHERE status='quarantined'"
            )
            quarantined = int(cur.fetchone()[0])
    except Exception as exc:
        return Status.FAIL, f'DB query failed: {exc}'
    if stuck > 0:
        return Status.FAIL, f'{stuck} chunk(s) stuck in_progress >24h, quarantined={quarantined}'
    if quarantined > 0:
        return Status.WARN, f'quarantined={quarantined}'
    return Status.PASS, f'no stuck chunks, quarantined=0'
