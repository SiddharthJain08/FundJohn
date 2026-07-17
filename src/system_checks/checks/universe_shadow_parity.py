"""universe_shadow_parity — SP-7 Phase C C1 flip gate (spec §3.5).

PASS  = last ≤3 shadow run-dates have ZERO universe-diff and zero
        resolve_error for every is_adopted=FALSE strategy.
WARN  = un-adopted drift found (data semantics — diagnose via
        added/removed tickers; remedies: widen default predicate, adopt
        the strategy, or fix category metadata). Blocks the C1 flip,
        EXCEPT the documented sub-floor case: removed names that have
        <60 bars in prices.parquet (clamp keeps them, the resolver floor
        excludes them; they can't fill strategy lookbacks so the spec's
        zero-SIGNAL-delta still holds). Classification SQL + decision
        rule: docs/runbooks/sp7-phase-c-runbook.md §3. Verified-empty 2026-06-07.
FAIL  = resolve_error rows present (the builder failed-open — code bug).
SKIP  = gate off / no DB.

Flip prereq (runbook §5): PASS, or WARN classified all-sub-floor per §3.
"""
import os

from ..registry import check
from ..types import Status


@check(name='universe_shadow_parity', tags=['strategies'], requires=['db'])
def _universe_shadow_parity():
    if os.environ.get('OPENCLAW_LIVE_UNIVERSE_SHADOW') != '1':
        return Status.SKIP, 'OPENCLAW_LIVE_UNIVERSE_SHADOW gate off'
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return Status.SKIP, 'POSTGRES_URI not set'
    import psycopg2
    with psycopg2.connect(uri) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT run_date FROM universe_shadow_parity "
                    "ORDER BY run_date DESC LIMIT 3")
        days = [r[0] for r in cur.fetchall()]
        if not days:
            return Status.WARN, 'no shadow rows yet (first gated cycle pending?)'
        cur.execute("""
            SELECT strategy_id, run_date, resolve_error,
                   jsonb_array_length(added_tickers)
                 + jsonb_array_length(removed_tickers) AS drift
              FROM universe_shadow_parity
             WHERE run_date = ANY(%s) AND is_adopted = FALSE
               AND (resolve_error IS NOT NULL
                    OR jsonb_array_length(added_tickers)
                     + jsonb_array_length(removed_tickers) > 0)
             ORDER BY drift DESC
        """, (days,))
        bad = cur.fetchall()
    errors = [b for b in bad if b[2]]
    if errors:
        worst = '; '.join(f'{b[0]}@{b[1]}' for b in errors[:3])
        return Status.FAIL, f'{len(errors)} resolve_error rows (code bug): {worst}'
    if bad:
        worst = '; '.join(f'{b[0]}@{b[1]} drift={b[3]}' for b in bad[:3])
        return Status.WARN, f'{len(bad)} un-adopted parity breaks in last {len(days)}d: {worst}'
    return Status.PASS, f'{len(days)} day(s) clean for un-adopted strategies'
