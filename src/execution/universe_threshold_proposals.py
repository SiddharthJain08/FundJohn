"""SP-7 Phase B B3 — breadth-scaled min_cumulative_sharpe proposals.

Union-N rule (spec §6): on each adoption event, N_union = |∪ adopted-universe
memberships across all registry-approved strategies| on the LATEST snapshot
(un-adopted strategies contribute sp500). factor = √(ln N_union / ln N_sp500).
proposed = current_base × factor per regime, clamped [1.0, 10.0].
Proposals only — NEVER writes regime_sizer_params (the :3000 Apply button does,
through the existing PUT validation).
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def breadth_factor(n_union: int, n_sp500: int) -> float:
    if n_union <= 1 or n_sp500 <= 1:
        return 1.0
    return math.sqrt(math.log(n_union) / math.log(n_sp500))


def propose_values(bases: dict[str, float], *, factor: float) -> dict[str, float]:
    return {r: max(1.0, min(10.0, round(b * factor, 2)))
            for r, b in bases.items()}


def _resolver():
    from src.strategies._db_adapters import PostgresMetadataDB
    from src.strategies.coverage_index import CoverageIndex
    from src.strategies.universe_resolver import UniverseResolver

    def manifest_loader():
        return json.loads(
            (ROOT / 'src' / 'strategies' / 'manifest.json').read_text())

    return UniverseResolver(
        db=PostgresMetadataDB(os.environ['POSTGRES_URI']),
        coverage=CoverageIndex.from_parquet('/root/openclaw/data/master/prices.parquet'),
        manifest_loader=manifest_loader)


def compute_union_n(pg, as_of: date | None = None) -> tuple[int, int]:
    """(N_union across approved strategies' current predicates, N_sp500)."""
    from src.strategies.universe_default import sp500
    from src.strategies.universe_resolver import MockResolver
    from src.strategies._db_adapters import PostgresMetadataDB, ParquetCoverage

    as_of = as_of or date.today()
    with pg.cursor() as cur:
        cur.execute("SELECT id FROM strategy_registry WHERE status='approved'")
        sids = [r[0] for r in cur.fetchall()]
    res = _resolver()
    union: set[str] = set()
    for sid in sids:
        try:
            union.update(res.resolve(sid, as_of))
        except Exception as e:
            print(f'[b3] resolve failed for {sid}: {e} — contributes nothing')
    base = MockResolver(db=PostgresMetadataDB(os.environ['POSTGRES_URI']),
                        coverage=ParquetCoverage(), predicate=sp500)
    n_sp500 = len(base.resolve('_sp500_baseline', as_of))
    return len(union), n_sp500


def compute_and_write(trigger: str) -> int:
    """Supersede pending proposals, write 4 fresh ones. Returns count."""
    import psycopg2
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        n_union, n_sp500 = compute_union_n(pg)
        factor = breadth_factor(n_union, n_sp500)
        with pg.cursor() as cur:
            cur.execute("""SELECT regime_state, min_cumulative_sharpe,
                                  liquidity_param, min_signal_notional_usd
                             FROM regime_sizer_params""")
            rows = {r[0]: {'min_cumulative_sharpe': float(r[1]),
                           'liquidity_param': float(r[2]) if r[2] is not None else None,
                           'min_signal_notional_usd': float(r[3]) if r[3] is not None else None}
                    for r in cur.fetchall()}
            bases = {r: v['min_cumulative_sharpe'] for r, v in rows.items()}
            proposed = propose_values(bases, factor=factor)
            cur.execute("""UPDATE universe_threshold_proposals
                              SET status='superseded', decided_at=NOW(),
                                  decided_by=%s,
                                  decision_reason='auto-superseded by newer proposal'
                            WHERE status='pending'""", (f'sp7b:{trigger}',))
            n = 0
            for regime in REGIMES:
                if regime not in bases:
                    continue
                if abs(proposed[regime] - bases[regime]) < 0.01:
                    continue  # no-op proposal adds noise
                cur.execute("""
                    INSERT INTO universe_threshold_proposals
                      (proposer, regime_state, current_row,
                       proposed_min_cumulative_sharpe, basis, status)
                    VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, 'pending')""",
                    (f'sp7b:{trigger}', regime, json.dumps(rows[regime]),
                     proposed[regime],
                     json.dumps({'n_union': n_union, 'n_sp500': n_sp500,
                                 'factor': round(factor, 4),
                                 'trigger': trigger})))
                n += 1
        pg.commit()
        print(f'[b3] trigger={trigger} N_union={n_union} N_sp500={n_sp500} '
              f'factor={factor:.4f} proposals={n}')
        return n
    finally:
        pg.close()


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'src'))
    sys.exit(0 if compute_and_write(
        trigger=sys.argv[1] if len(sys.argv) > 1 else 'manual') >= 0 else 1)
