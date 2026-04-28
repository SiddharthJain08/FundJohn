#!/usr/bin/env python3
"""migrate_paper_to_candidate.py — one-shot manifest rewrite for the fused-
staging-approval lifecycle (2026-04-27).

Under the new lifecycle, `paper` is a frozen legacy state: every existing
PAPER strategy is the equivalent of the new CANDIDATE state. Run this script
once after migration 060_paper_status_rename.sql.

What it does:
  1. Acquires the cross-process manifest lock (src/strategies/_manifest_lock.py).
  2. For every record with state='paper', sets state='candidate', appends a
     transition history entry, and writes back atomically.
  3. Inserts a matching row into the lifecycle_events Postgres table.

It bypasses LifecycleStateMachine.transition() — the new
STRATEGY_VALID_TRANSITIONS table no longer contains (paper, candidate), and
adding a temporary escape hatch would be brittle.

Idempotent: skips strategies whose last history entry is already the
migration entry (so re-running this is a no-op).

Usage:
    python3 scripts/migrate_paper_to_candidate.py [--dry-run] [--manifest PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

OPENCLAW_DIR = Path(os.environ.get('OPENCLAW_DIR', Path(__file__).resolve().parents[1]))
DEFAULT_MANIFEST = OPENCLAW_DIR / 'src' / 'strategies' / 'manifest.json'

MIGRATION_ACTOR  = 'migration:fused_approval'
MIGRATION_REASON = 'paper renamed to candidate (fused-approval lifecycle 2026-04-27)'


def _import_manifest_lock():
    sys.path.insert(0, str(OPENCLAW_DIR / 'src' / 'strategies'))
    import _manifest_lock  # type: ignore
    return _manifest_lock


def _already_migrated(rec: dict) -> bool:
    history = rec.get('history') or []
    if not history:
        return False
    last = history[-1]
    return (
        last.get('actor')      == MIGRATION_ACTOR and
        last.get('to_state')   == 'candidate' and
        last.get('from_state') == 'paper'
    )


def _migrate_record(rec: dict, now: str) -> bool:
    """Mutate rec in place. Returns True if changed."""
    if rec.get('state') != 'paper':
        return False
    if _already_migrated(rec):
        return False
    rec['history'] = rec.get('history') or []
    rec['history'].append({
        'from_state': 'paper',
        'to_state':   'candidate',
        'timestamp':  now,
        'actor':      MIGRATION_ACTOR,
        'reason':     MIGRATION_REASON,
        'metadata':   {},
    })
    rec['state'] = 'candidate'
    rec['state_since'] = now
    return True


def _persist_lifecycle_events(strategy_ids: list[str], now: str) -> int:
    uri = os.environ.get('POSTGRES_URI')
    if not uri or not strategy_ids:
        return 0
    try:
        import psycopg2  # type: ignore
    except ImportError:
        print('[migrate_paper] psycopg2 not available — skipping lifecycle_events insert')
        return 0
    written = 0
    skipped_no_registry = 0
    conn = psycopg2.connect(uri)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for sid in strategy_ids:
            # Skip orphans not present in strategy_registry — the FK on
            # lifecycle_events.strategy_id would reject them anyway, and the
            # manifest entry has already been migrated by the time we reach here.
            cur.execute('SELECT 1 FROM strategy_registry WHERE id = %s', (sid,))
            if not cur.fetchone():
                skipped_no_registry += 1
                continue
            # Idempotency check: skip if a matching migration row already exists.
            cur.execute(
                """SELECT 1 FROM lifecycle_events
                    WHERE strategy_id = %s AND actor = %s
                      AND from_state = 'paper' AND to_state = 'candidate'
                    LIMIT 1""",
                (sid, MIGRATION_ACTOR),
            )
            if cur.fetchone():
                continue
            cur.execute(
                """INSERT INTO lifecycle_events
                     (strategy_id, from_state, to_state, actor, reason, metadata, occurred_at)
                   VALUES (%s, 'paper', 'candidate', %s, %s, '{}'::jsonb, %s)""",
                (sid, MIGRATION_ACTOR, MIGRATION_REASON, now),
            )
            written += 1
    finally:
        conn.close()
    if skipped_no_registry:
        print(f'[migrate_paper] lifecycle_events: skipped {skipped_no_registry} orphan(s) not in strategy_registry')
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', default=str(DEFAULT_MANIFEST))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f'[migrate_paper] manifest not found at {manifest_path}', file=sys.stderr)
        return 1

    if args.dry_run:
        m = json.loads(manifest_path.read_text())
        candidates = [
            sid for sid, rec in (m.get('strategies') or {}).items()
            if rec.get('state') == 'paper' and not _already_migrated(rec)
        ]
        print(f'[migrate_paper] dry-run: would migrate {len(candidates)} strategy(ies):')
        for sid in candidates:
            print(f'  - {sid}')
        return 0

    _ml = _import_manifest_lock()
    now = datetime.now(timezone.utc).isoformat()
    migrated_ids: list[str] = []

    def _rewrite(m: dict) -> dict:
        m = m or {}
        strategies = m.setdefault('strategies', {})
        for sid, rec in strategies.items():
            if _migrate_record(rec, now):
                migrated_ids.append(sid)
        if migrated_ids:
            m['updated_at'] = now
        return m

    _ml.with_manifest_lock(manifest_path, _rewrite, actor=MIGRATION_ACTOR)
    print(f'[migrate_paper] manifest: migrated {len(migrated_ids)} strategy(ies)')
    for sid in migrated_ids:
        print(f'  - {sid}')

    written = _persist_lifecycle_events(migrated_ids, now)
    print(f'[migrate_paper] lifecycle_events: inserted {written} row(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
