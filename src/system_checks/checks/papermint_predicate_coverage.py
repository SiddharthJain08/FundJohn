"""Agents-tagged check: % of recent research_candidates that emitted an inferred predicate.

Gate: OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1. When the gate is off the check always
returns PASS so it does not clutter maintenance runs when the feature is disabled.
"""
from __future__ import annotations

import os

import psycopg2

from ..registry import check
from ..types import Status


@check(name='papermint_predicate_coverage', tags=['agents', 'strategies'], requires=['db'])
def _papermint_predicate_coverage():
    """Check that PaperHunter is emitting inferred_universe_filter in hunter_result_json.

    PASS when gate off. PASS if fewer than 5 new candidates in last 30d (too few to judge).
    FAIL if 0% predicate coverage (prompt regression?). WARN if < 20%.
    PASS otherwise.
    """
    if os.environ.get('OPENCLAW_PHASE_D_PREDICATE_AT_MINT') != '1':
        return Status.PASS, 'gate off; n/a'
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return Status.FAIL, 'POSTGRES_URI not set'
    try:
        with psycopg2.connect(uri) as c, c.cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE hunter_result_json->>'inferred_universe_filter' IS NOT NULL
                                     AND hunter_result_json->>'inferred_universe_filter' != 'null'),
                  COUNT(*) FILTER (WHERE hunter_result_json IS NOT NULL)
                FROM research_candidates
                WHERE submitted_at > NOW() - INTERVAL '30 days'
            """)
            with_pred, total = cur.fetchone()
    except Exception as exc:
        return Status.FAIL, f'DB query failed: {exc}'
    if total < 5:
        return Status.PASS, f'too few new candidates ({total}) — n/a'
    pct = 100.0 * with_pred / total
    if pct == 0:
        return Status.FAIL, '0% of 30d candidates emitted predicate (prompt regression?)'
    if pct < 20:
        return Status.WARN, f'only {pct:.0f}% predicate coverage in 30d'
    return Status.PASS, f'{pct:.0f}% coverage ({with_pred}/{total})'
