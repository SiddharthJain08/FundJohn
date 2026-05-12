#!/usr/bin/env python3
"""One-time cleanup: remove the deprecated `eligible_regimes` field from
every strategy entry in manifest.json. Authoritative source is now
strategy_regime_params (DB). Run once during Phase 2C rollout.

Safety: only removes the field for strategies that ALREADY have all 4
canonical regime rows present in strategy_regime_params (i.e., the seed
migration covered them). Strategies missing rows are skipped + reported.

Atomic write under manifest_lock — same cross-process lock used by
lifecycle.py and saturday_brain.js.

Usage:
    python3 scripts/cleanup_manifest_eligibility_field.py
    python3 scripts/cleanup_manifest_eligibility_field.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'
REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _strategies_with_full_regime_coverage() -> set:
    """Return strategy_ids that have all 4 regime rows in strategy_regime_params."""
    import psycopg2
    with psycopg2.connect(_db_uri()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, ARRAY_AGG(regime_state ORDER BY regime_state)
                  FROM strategy_regime_params
                 GROUP BY strategy_id
            """)
            out = set()
            for sid, regimes in cur.fetchall():
                if set(regimes) >= set(REGIMES):
                    out.add(sid)
            return out


def cleanup(dry_run: bool = False) -> dict:
    covered = _strategies_with_full_regime_coverage()
    manifest_text = MANIFEST.read_text(encoding='utf-8')
    manifest = json.loads(manifest_text)
    strategies = manifest.get('strategies', {}) or {}
    removed = []
    skipped_uncovered = []
    for sid, rec in strategies.items():
        if rec.get('eligible_regimes') is None:
            continue
        if sid not in covered:
            skipped_uncovered.append(sid)
            continue
        removed.append((sid, rec.pop('eligible_regimes')))
    if not removed:
        return {'removed': [], 'skipped_uncovered': skipped_uncovered,
                'note': 'no manifest entries carry the field; nothing to do'}

    if dry_run:
        return {'removed': removed, 'skipped_uncovered': skipped_uncovered,
                'dry_run': True}

    # Write under cross-process lock via lifecycle.save_manifest's _manifest_lock helper.
    # We can't use lifecycle directly because we want to write the raw manifest
    # (skipping the StrategyRecord round-trip). Use _manifest_lock directly.
    sys.path.insert(0, str(ROOT / 'src'))
    try:
        from strategies import _manifest_lock as _ml
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_manifest_lock",
            str(ROOT / 'src' / 'strategies' / '_manifest_lock.py'),
        )
        _ml = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_ml)

    def _merge(disk: dict) -> dict:
        for sid, _ in removed:
            if sid in (disk.get('strategies') or {}):
                disk['strategies'][sid].pop('eligible_regimes', None)
        return disk

    _ml.with_manifest_lock(MANIFEST, _merge, actor='cleanup-manifest-eligibility-field')
    return {'removed': removed, 'skipped_uncovered': skipped_uncovered}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    result = cleanup(dry_run=args.dry_run)
    print(f'removed: {len(result["removed"])} strategies')
    for sid, prev in result['removed']:
        print(f'  - {sid}: was {prev}')
    if result['skipped_uncovered']:
        print(f'\nSKIPPED (no full regime coverage in strategy_regime_params):')
        for sid in result['skipped_uncovered']:
            print(f'  - {sid}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
