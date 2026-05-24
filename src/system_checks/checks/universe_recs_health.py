"""Agents-tagged check: strategy_universe_recommendations written within 14 calendar days.

Gate: OPENCLAW_UNIVERSE_RECS=1. When the gate is off the check always returns
PASS so it does not clutter maintenance runs when the feature is disabled.
"""
from __future__ import annotations

import os

import psycopg2

from ..registry import check
from ..types import Status


@check(name='universe_recs_health', tags=['agents', 'strategies'], requires=['db'])
def _universe_recs_health():
    """Verify strategy_universe_recommendations was written within last 14 days.
    PASS when gate off. FAIL if 0 recs in last 14d. WARN if <3 recs in last 14d.
    PASS otherwise.

    This check is intentionally count-based (recs in last 14d) — it answers the
    operational health question "did the recommender produce output recently?".
    The companion time-based check `_check_universe_recs_freshness` in
    src/maintenance/doctor.py uses the timestamp of the latest rec directly;
    that check targets sub-second pre-flight use (different audience/latency
    budget) while this check targets the daily maintenance health digest.
    """
    if os.environ.get('OPENCLAW_UNIVERSE_RECS') != '1':
        return Status.PASS, 'gate off; n/a'
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return Status.FAIL, 'POSTGRES_URI not set'
    try:
        with psycopg2.connect(uri) as c, c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM strategy_universe_recommendations "
                "WHERE recommended_at >= NOW() - INTERVAL '14 days'"
            )
            count = cur.fetchone()[0]
    except Exception as exc:
        return Status.FAIL, f'DB query failed: {exc}'
    if count == 0:
        return Status.FAIL, 'no universe recs in last 14d — timer may not have run'
    if count < 3:
        return Status.WARN, f'only {count} universe recs in last 14d (expected ≥3)'
    return Status.PASS, f'{count} universe recs in last 14d'
