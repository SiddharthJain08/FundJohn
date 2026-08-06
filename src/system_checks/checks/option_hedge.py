"""Pipeline-tagged check: SP-5.1b-ii option delta-hedge health.

Gate OFF -> SKIP. Gate ON -> assert the EOD pipeline gates are on + the ledger is
reachable; report active-hedge count + flag STALE-TARGET rows (a live structure
whose hedge target wasn't recomputed in the last trading day). Read-only.

Known limitation: the staleness window uses a calendar-day comparison
(CURRENT_DATE - INTERVAL '1 day').  An active hedge last rehedged on Friday
will appear STALE on Monday morning before the first cycle runs.  This is
intentional — the operator inspects and re-runs the hedge step — but callers
should be aware of weekend false-positives.
"""
from __future__ import annotations

import os

from ..registry import check
from ..types import Status


@check(name='option_hedge', tags=['pipeline'], requires=['fs'])
def _option_hedge():
    if os.environ.get('OPENCLAW_OPTION_DELTA_HEDGE') != '1':
        return Status.SKIP, 'OPENCLAW_OPTION_DELTA_HEDGE=0'

    from execution.signal_target_mode import eod_register_on   # §8 alias resolver
    if (not eod_register_on()
            or os.environ.get('OPENCLAW_EOD_RECONCILE') != '1'):
        return Status.FAIL, (
            'delta-hedge ON but EOD pipeline gates off '
            '(targets never reconcile/fill)'
        )

    import psycopg2
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return Status.SKIP, 'POSTGRES_URI not set'

    try:
        with psycopg2.connect(uri, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*),
                              COUNT(*) FILTER (
                                  WHERE last_rehedge_date IS NULL
                                     OR last_rehedge_date < (CURRENT_DATE - INTERVAL '1 day')
                              )
                       FROM option_hedge_ledger
                       WHERE status = 'active'"""
                )
                total, stale = cur.fetchone()
    except Exception as exc:
        return Status.FAIL, f'ledger query failed: {type(exc).__name__}: {exc}'[:200]

    if stale and stale > 0:
        return (
            Status.FAIL,
            f'active_hedges={total} STALE_TARGET={stale} '
            f'(hedge target not recomputed recently)',
        )
    return Status.PASS, f'active_hedges={total} stale=0'
