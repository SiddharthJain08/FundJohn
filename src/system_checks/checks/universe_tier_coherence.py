"""SP-7 Phase B — tier-coherence guard for ticker_metadata_snapshots.

Catches the v1/v2 ghost-row class (mega-caps absent from rank tiers) and
degenerate daily snapshots (rank flags never computed). See spec
docs/superpowers/specs/2026-06-06-sp7-phase-b-tier-ladder-design.md §3.
"""
from __future__ import annotations
import os

import psycopg2

from ..registry import check
from ..types import Status

MEGA_CAPS = ('AAPL', 'MSFT', 'NVDA', 'JPM')
PROBE_MONTHS = ('2021-07-31', '2023-06-30', '2025-06-30')


@check(name='universe_tier_coherence', tags=['strategies'], requires=['db'])
def _universe_tier_coherence():
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL', '')
    if not uri:
        return Status.FAIL, 'POSTGRES_URI not set'
    conn = psycopg2.connect(uri)
    try:
        cur = conn.cursor()
        problems = []
        # 1) mega-caps must be in_r1000 at every probe month (resolver's exact query)
        for snap in PROBE_MONTHS:
            cur.execute("""
                SELECT symbol FROM (
                  SELECT DISTINCT ON (symbol) symbol, in_r1000
                  FROM ticker_metadata_snapshots
                  WHERE snapshot_date <= %s AND symbol = ANY(%s)
                  ORDER BY symbol, snapshot_date DESC) t
                WHERE NOT in_r1000""", (snap, list(MEGA_CAPS)))
            missing = [r[0] for r in cur.fetchall()]
            if missing:
                problems.append(f'{snap}: {missing} not in_r1000')
        # 2) recent degenerate-daily detector: any snapshot in last 30d with
        #    >1000 rows where zero rows have in_r3000
        cur.execute("""
            SELECT snapshot_date FROM ticker_metadata_snapshots
            WHERE snapshot_date > CURRENT_DATE - 30
            GROUP BY snapshot_date
            HAVING count(*) > 1000 AND count(*) FILTER (WHERE in_r3000) = 0
            ORDER BY snapshot_date""")
        degenerate = [str(r[0]) for r in cur.fetchall()]
        if degenerate:
            problems.append(f'degenerate dailies (r3000=0): {degenerate[:5]}')
        if problems:
            return Status.FAIL, '; '.join(problems)[:200]
        return Status.PASS, (f'mega-caps in_r1000 at {len(PROBE_MONTHS)} probe months; '
                             'no degenerate dailies in 30d')
    except Exception as e:
        return Status.ERROR, f'tier-coherence sweep failed: {e}'
    finally:
        conn.close()
