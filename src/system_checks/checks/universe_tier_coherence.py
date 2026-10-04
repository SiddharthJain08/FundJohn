"""SP-7 Phase B — tier-coherence guard for ticker_metadata_snapshots.

Catches the v1/v2 ghost-row class (mega-caps absent from rank tiers) and
degenerate daily snapshots (rank flags never computed). See spec
docs/archive/superpowers/specs/2026-06-06-sp7-phase-b-tier-ladder-design.md §3.
"""
from __future__ import annotations
import os

import psycopg2

from ..registry import check
from ..types import Status

MEGA_CAPS = ('AAPL', 'MSFT', 'NVDA', 'JPM')
PROBE_MONTHS = ('2021-07-31', '2023-06-30', '2025-06-30')


STOCK_TIER_BASE = {'stocks_sp500': 'sp500', 'stocks_r1000': 'tier_r1000',
                   'stocks_r3000': 'tier_r3000', 'stocks_liquid': 'tier_liquid'}


def stock_tier_problems(df, types: dict) -> list:
    """Pure: for a membership-artifact frame (run_id, tier, snapshot_date,
    symbols) assert, per snapshot, stocks_X subset-of tier_X and that no symbol
    typed etf/fund (types = {symbol: security_type}) is in any stocks_* tier.
    An artifact with no stocks_* rows (built before Phase 1) yields []."""
    probs = []
    by = {(r.tier, str(r.snapshot_date)): set(r.symbols) for r in df.itertuples()}
    for (tier, snap), members in sorted(by.items()):
        base = STOCK_TIER_BASE.get(tier)
        if base is None:
            continue
        extra = members - by.get((base, snap), set())
        if extra:
            probs.append(f'{tier}@{snap}: {len(extra)} not in {base}')
        funds = sorted(s for s in members if types.get(s) in ('etf', 'fund'))
        if funds:
            probs.append(f'{tier}@{snap}: {len(funds)} etf/fund members e.g. {funds[:3]}')
    return probs


def _artifact_problems(cur) -> list:
    """Newest membership artifact vs the DB's latest security types. Skipped
    (no problems) when there is no artifact, no stocks_* rows, or migration 163
    has not been applied."""
    import glob
    from pathlib import Path
    import pandas as pd
    root = Path(__file__).resolve().parents[3]
    arts = sorted(glob.glob(str(root / 'data' / 'universe_tier_membership_*.parquet')))
    if not arts:
        return []
    df = pd.read_parquet(arts[-1])
    if not df['tier'].isin(list(STOCK_TIER_BASE)).any():
        return []
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name="
                "'ticker_metadata_snapshots' AND column_name='security_type'")
    if cur.fetchone() is None:
        return []
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, security_type
                   FROM ticker_metadata_snapshots WHERE security_type IS NOT NULL
                   ORDER BY symbol, snapshot_date DESC""")
    types = {s: t for s, t in cur.fetchall()}
    return [f'{Path(arts[-1]).name}: {p}' for p in stock_tier_problems(df, types)]


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
        # 3) stocks_* membership tiers (security type Phase 1)
        problems.extend(_artifact_problems(cur))
        if problems:
            return Status.FAIL, '; '.join(problems)[:200]
        return Status.PASS, (f'mega-caps in_r1000 at {len(PROBE_MONTHS)} probe months; '
                             'no degenerate dailies in 30d')
    except Exception as e:
        return Status.ERROR, f'tier-coherence sweep failed: {e}'
    finally:
        conn.close()
