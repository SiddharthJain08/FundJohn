#!/usr/bin/env python3
"""
Reconcile strategy_registry against src/strategies/manifest.json.

For every manifest strategy in state 'live' or 'paper':
- INSERT a row into strategy_registry as status='approved' if missing.
- UPDATE status → 'approved' if present with a different status.

Manifest candidate/staging strategies are left untouched (they are pre-promotion
or awaiting data by design).

The script refuses to touch a strategy whose implementation isn't registered in
registry._IMPL_MAP. Safety: engine.py::load_approved_strategies would silently
drop those anyway, so approving them would be a lie.

Idempotent — safe to re-run.

Usage:
    POSTGRES_URI=... python3 scripts/reconcile_strategy_registry.py [--dry-run] [--only STRATEGY_ID]
"""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies.registry import list_registered_ids  # noqa: E402


def load_manifest():
    path = ROOT / 'src' / 'strategies' / 'manifest.json'
    return json.loads(path.read_text())


def active_strategies(manifest):
    return [
        (sid, rec)
        for sid, rec in (manifest.get('strategies') or {}).items()
        if rec.get('state') in ('live', 'paper')
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--only', help='Restrict to a single strategy_id')
    args = ap.parse_args()

    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        print('POSTGRES_URI not set'); sys.exit(1)

    manifest = load_manifest()
    registered = set(list_registered_ids())
    active = active_strategies(manifest)
    if args.only:
        active = [(s, r) for s, r in active if s == args.only]
        if not active:
            print(f'{args.only} not found in manifest live/paper states'); sys.exit(1)

    conn = psycopg2.connect(uri)
    cur = conn.cursor()
    inserts = []
    updates = []
    skipped_missing_impl = []

    try:
        existing = {
            r[0]: r[1]
            for r in cur.execute("SELECT id, status FROM strategy_registry") or cur.fetchall()
        }
    except Exception:
        pass
    cur.execute("SELECT id, status FROM strategy_registry")
    existing = {row[0]: row[1] for row in cur.fetchall()}

    for sid, rec in active:
        if sid not in registered:
            skipped_missing_impl.append(sid)
            continue
        desc = (rec.get('metadata') or {}).get('description') or ''
        impl_path = f'src/strategies/implementations/{sid}.py'
        if sid not in existing:
            inserts.append((sid, desc, impl_path))
        elif existing[sid] != 'approved':
            updates.append((sid, existing[sid]))

    print(f'Inserts planned:         {len(inserts)}')
    for sid, _, _ in inserts:
        print(f'  + {sid}')
    print(f'Status flips planned:    {len(updates)}')
    for sid, old in updates:
        print(f'  * {sid} ({old} → approved)')
    if skipped_missing_impl:
        print(f'Skipped (no _IMPL_MAP):  {len(skipped_missing_impl)}')
        for sid in skipped_missing_impl:
            print(f'  - {sid}')

    if args.dry_run:
        print('\nDry-run — no DB writes.')
        return

    for sid, desc, impl_path in inserts:
        cur.execute("""
            INSERT INTO strategy_registry
              (id, name, description, tier, status, implementation_path, created_at)
            VALUES (%s, %s, %s, 2, 'approved', %s, NOW())
            ON CONFLICT (id) DO NOTHING
        """, (sid, sid, desc[:500], impl_path))

    for sid, _old in updates:
        cur.execute("""
            UPDATE strategy_registry
            SET status='approved', approved_at=NOW()
            WHERE id=%s
        """, (sid,))

    conn.commit()
    cur.close()
    conn.close()
    print(f'\nApplied {len(inserts)} inserts and {len(updates)} status flips.')


if __name__ == '__main__':
    main()
