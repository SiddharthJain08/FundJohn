#!/usr/bin/env python3
"""One-time idempotent seed of strategy_regime_params from manifest.json.

Reads current manifest.eligible_regimes (or absence) and writes one row
per (strategy, canonical_regime) with backward-compat semantics:
  - eligible_regimes is None / missing → eligible = True for all 4 regimes
  - eligible_regimes is []             → eligible = True for all 4 regimes
                                          (matches Phase 1 gate backward-compat)
  - eligible_regimes is list           → eligible = (regime in list)

Idempotent via ON CONFLICT DO NOTHING. Re-running inserts zero new rows
once seeded.

Usage:
    python3 scripts/seed_strategy_regime_params.py
    python3 scripts/seed_strategy_regime_params.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'
TODAY = datetime.now(timezone.utc).date().isoformat()


def compute_rows(manifest: dict) -> list[dict]:
    rows: list[dict] = []
    tag = f'migration:from-manifest-{TODAY}'
    for sid, rec in (manifest.get('strategies') or {}).items():
        declared = rec.get('eligible_regimes')
        for regime in CANONICAL_REGIMES:
            if declared is None or declared == []:
                eligible = True
            else:
                eligible = regime in declared
            rows.append({
                'strategy_id':  sid,
                'regime_state': regime,
                'eligible':     eligible,
                'set_by':       tag,
            })
    return rows


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def upsert(rows: list[dict]) -> int:
    import psycopg2
    sql = """
        INSERT INTO strategy_regime_params
            (strategy_id, regime_state, eligible, set_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (strategy_id, regime_state) DO NOTHING
    """
    n_inserted = 0
    with psycopg2.connect(_db_uri()) as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(sql, (r['strategy_id'], r['regime_state'],
                                   r['eligible'], r['set_by']))
                n_inserted += cur.rowcount
        conn.commit()
    return n_inserted


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    rows = compute_rows(manifest)
    print(f'computed {len(rows)} rows from '
          f'{len(manifest.get("strategies") or {})} strategies')
    if args.dry_run:
        print('[dry-run] no rows written')
        return 0
    inserted = upsert(rows)
    print(f'[seed] inserted {inserted} new rows (existing rows untouched)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
