"""Agents-tagged check: % of recent research_candidates that emitted an inferred predicate.

Gate: OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1. When the gate is off the check always
returns PASS so it does not clutter maintenance runs when the feature is disabled.
"""
from __future__ import annotations

import os

import psycopg2

from ..registry import check
from ..types import Status

# SP-2 Phase D shipped on this date — paperhunter prompt began including the
# `inferred_universe_filter` field at Step 7 from this point. Candidates
# submitted before this date were extracted by the prior prompt and DON'T
# carry the field; including them in the denominator skews the metric down.
# After 30 days have elapsed (2026-06-24+) the NOW()-30d window naturally
# excludes them, so this floor becomes a no-op.
PHASE_D_DEPLOY_DATE = '2026-05-25'


@check(name='papermint_predicate_coverage', tags=['agents', 'strategies'], requires=['db'])
def _papermint_predicate_coverage():
    """Check that PaperHunter is emitting inferred_universe_filter in hunter_result_json.

    Scope: kind='paper' candidates submitted after Phase D shipped. Excludes
    kind='internal' (strategist-ideator drafts that bypass paperhunter entirely
    and carry a strategy_spec-shaped JSON in hunter_result_json — they're not
    expected to emit the predicate).

    PASS when gate off. PASS if fewer than 20 in-scope candidates in 30d
    (too few to judge). FAIL if 0%. WARN if < 20%. PASS otherwise.
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
                WHERE kind = 'paper'
                  AND submitted_at > GREATEST(NOW() - INTERVAL '30 days', %s::date)
            """, (PHASE_D_DEPLOY_DATE,))
            with_pred, total = cur.fetchone()
    except Exception as exc:
        return Status.FAIL, f'DB query failed: {exc}'
    if total < 20:
        return Status.PASS, f'too few in-scope paper candidates ({total}) — n/a'
    pct = 100.0 * with_pred / total
    if pct == 0:
        return Status.FAIL, '0% of in-scope candidates emitted predicate (prompt regression?)'
    if pct < 20:
        return Status.WARN, f'only {pct:.0f}% predicate coverage ({with_pred}/{total})'
    return Status.PASS, f'{pct:.0f}% coverage ({with_pred}/{total})'
