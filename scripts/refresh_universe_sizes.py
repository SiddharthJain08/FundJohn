#!/usr/bin/env python3
"""Refresh strategy_universe_sizes (migration 141): N = |UniverseResolver.resolve(
sid, today)| per registry-approved strategy — the size of the ticker universe the
strategy's predicate acts on. Feeds the sizer's √(ln N) breadth weight factor
(OPENCLAW_STRATEGY_BREADTH_WEIGHT).

DECOUPLED from the weekly strategy_weights rebuild on purpose: a resolver failure
must never break live sizing. Safe to re-run (UPSERT). Run as a one-time backfill
and on a light schedule (universes only move on adoption / snapshot growth).

Mirrors src/execution/universe_threshold_proposals.py:_resolver — same resolver,
same as-of resolution SP-7 uses for N.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _resolver():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'src'))
    from src.strategies._db_adapters import PostgresMetadataDB
    from src.strategies.coverage_index import CoverageIndex
    from src.strategies.universe_resolver import UniverseResolver

    def manifest_loader():
        return json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())

    return UniverseResolver(
        db=PostgresMetadataDB(os.environ['POSTGRES_URI']),
        coverage=CoverageIndex.from_parquet(str(ROOT / 'data' / 'master' / 'prices.parquet')),
        manifest_loader=manifest_loader)


def refresh(as_of: date | None = None, verbose: bool = True) -> int:
    import psycopg2
    as_of = as_of or date.today()
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT id FROM strategy_registry WHERE status='approved'")
            sids = [r[0] for r in cur.fetchall()]
        res = _resolver()
        written = 0
        with pg.cursor() as cur:
            for sid in sids:
                try:
                    size = len(res.resolve(sid, as_of))
                except Exception as e:  # never let one strategy abort the sweep
                    print(f'[universe-size] resolve failed for {sid}: {e} — skipped')
                    continue
                cur.execute("""
                    INSERT INTO strategy_universe_sizes (strategy_id, universe_size, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (strategy_id) DO UPDATE
                      SET universe_size = EXCLUDED.universe_size, updated_at = NOW()
                """, (sid, size))
                written += 1
                if verbose:
                    print(f'  {sid}: N={size}')
        pg.commit()
        print(f'[universe-size] refreshed {written}/{len(sids)} approved strategies as_of {as_of}')
        return written
    finally:
        pg.close()


if __name__ == '__main__':
    sys.exit(0 if refresh() >= 0 else 1)
