"""Pipeline check — research_candidates rows stuck in 'processing'.

A row stuck in 'processing' is one whose claim was made by the
research-orchestrator but the hunt never completed (crash mid-flight).
0 stuck rows is the normal state; this check warns when the count
exceeds operational thresholds so maintenance can trigger
`scripts/recover_stuck_processing.py`.

Thresholds (both trigger WARN — either condition is sufficient):
  - COUNT(*) WHERE status='processing' > 5   (unexpected backlog)
  - Oldest processing row age > 24 h         (long-term stall)
"""
from __future__ import annotations

import os

import psycopg2

from ..registry import check
from ..types import Status


@check(name='research_processing_stuck', tags=['pipeline'], requires=['db'])
def _research_processing_stuck():
    """WARN if research_candidates has >5 processing rows or oldest row > 24h old."""
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return Status.FAIL, 'POSTGRES_URI not set'
    try:
        with psycopg2.connect(uri) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*) AS cnt,
                  EXTRACT(EPOCH FROM (NOW() - MIN(submitted_at))) / 3600.0 AS oldest_hours
                FROM research_candidates
                WHERE status = 'processing'
            """)
            cnt, oldest_hours = cur.fetchone()
    except Exception as exc:
        return Status.FAIL, f'DB query failed: {exc}'

    cnt = cnt or 0
    oldest_hours = float(oldest_hours or 0)

    if cnt == 0:
        return Status.PASS, 'no rows in processing'

    issues = []
    if cnt > 5:
        issues.append(f'{cnt} rows stuck in processing (>5)')
    if oldest_hours > 24:
        issues.append(f'oldest processing row is {oldest_hours:.1f}h old (>24h)')

    if issues:
        hint = '; run scripts/recover_stuck_processing.py'
        return Status.WARN, '; '.join(issues) + hint

    return Status.PASS, f'{cnt} row(s) in processing (within thresholds)'
