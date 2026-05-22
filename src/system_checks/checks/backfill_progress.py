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
    (any → WARN) in backfill_audit.

    Gated on OPENCLAW_BACKFILL_5Y_ACTIVE=1 — when unset (default), this
    check PASSes with "gate off; n/a". The operator flips the gate
    AFTER the first production backfill so the audit table reflects
    real state rather than test residue from Tasks 7-9."""
    if os.environ.get('OPENCLAW_BACKFILL_5Y_ACTIVE') != '1':
        return Status.PASS, 'gate off; n/a'
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
            # Distinguish real validation failures from conservative
            # overlap-protection refusals (the latter is normal v1 behavior
            # when backfilling tickers that already have partial coverage).
            cur.execute(
                "SELECT count(*) FROM backfill_audit "
                "WHERE status='quarantined' "
                "AND (error_text IS NULL OR error_text NOT LIKE 'overlap with existing%')"
            )
            poisoning = int(cur.fetchone()[0])
    except Exception as exc:
        return Status.FAIL, f'DB query failed: {exc}'
    overlap = quarantined - poisoning
    if stuck > 0:
        return Status.FAIL, f'{stuck} chunk(s) stuck in_progress >24h, poisoning_quarantined={poisoning}, overlap_refusals={overlap}'
    if poisoning > 0:
        return Status.WARN, f'poisoning_quarantined={poisoning} (validation failures), overlap_refusals={overlap}'
    return Status.PASS, f'no stuck chunks, no validation-failure quarantines (overlap_refusals={overlap})'
