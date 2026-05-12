#!/usr/bin/env python3
"""Safe writer for manifest.json eligible_regimes + audit trail.

All edits go through set_eligibility(), which:
  1. validates inputs (canonical regimes, non-empty, known strategy)
  2. writes audit row to regime_eligibility_changes first
  3. atomically rewrites manifest.json (tmp + rename)
  4. on any failure, leaves manifest untouched

The gate (`src/strategies/regime_gate.py`) re-reads manifest.json on every
`is_eligible()` call, so changes apply on the next strategy invocation
without a service restart.

CLI:
    python -m strategies.eligibility_manager --list
    python -m strategies.eligibility_manager --set momentum_a LOW_VOL TRANSITIONING \
        --actor "operator:sid" --reason "trim HIGH_VOL after 90d drawdown" \
        --source "live_90d_sharpe=-0.6"
    python -m strategies.eligibility_manager --audit --limit 20
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
DEFAULT_MANIFEST = Path(__file__).resolve().parent / 'manifest.json'


def _db_uri() -> str:
    return (
        os.environ.get('DATABASE_URL')
        or os.environ.get('POSTGRES_URI')
        or 'postgresql://openclaw:password@localhost:5432/openclaw'
    )


def _connect(uri: str):
    import psycopg2
    return psycopg2.connect(uri)


def _insert_audit(*, actor: str, strategy_id: str,
                   before_regimes, after_regimes,
                   reason: str, source: str) -> None:
    with _connect(_db_uri()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO regime_eligibility_changes
                  (actor, strategy_id, before_regimes, after_regimes, reason, source)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (actor, strategy_id, before_regimes, after_regimes, reason, source))
        conn.commit()


def _query_audit(limit: int) -> list[dict]:
    with _connect(_db_uri()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT changed_at, actor, strategy_id,
                       before_regimes, after_regimes, reason, source
                  FROM regime_eligibility_changes
                 ORDER BY changed_at DESC
                 LIMIT %s
            """, (limit,))
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _canonicalize(regimes: list[str]) -> list[str]:
    """Dedupe and sort to canonical regime order. Validates entries."""
    seen = set()
    for r in regimes:
        if r not in CANONICAL_REGIMES:
            raise ValueError(f'invalid regime {r!r}; must be one of {CANONICAL_REGIMES}')
        seen.add(r)
    return [r for r in CANONICAL_REGIMES if r in seen]


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON via tmp + rename so the file is never half-written."""
    body = json.dumps(data, indent=2) + '\n'
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix='.manifest.',
                                    suffix='.tmp')
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(body)
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def set_eligibility(*, strategy_id: str, new_regimes: list[str],
                     actor: str, reason: str, source: str,
                     manifest_path: Path | None = None) -> dict:
    """Update one strategy's eligible_regimes. Audits then writes.

    Raises:
        KeyError: strategy_id not in manifest
        ValueError: invalid or empty regime list
        RuntimeError: audit insert failed (manifest unchanged)
    """
    manifest_path = manifest_path or DEFAULT_MANIFEST
    canonical = _canonicalize(new_regimes)
    if not canonical:
        raise ValueError('eligible_regimes must contain at least one valid regime')

    data = json.loads(manifest_path.read_text())
    strategies = data.setdefault('strategies', {})
    if strategy_id not in strategies:
        raise KeyError(strategy_id)
    record = strategies[strategy_id]
    before = record.get('eligible_regimes')

    # 1) audit row first — if this fails, the manifest must not change.
    _insert_audit(actor=actor, strategy_id=strategy_id,
                  before_regimes=before, after_regimes=canonical,
                  reason=reason, source=source)

    # 2) only mutate after audit landed.
    record['eligible_regimes'] = canonical
    _atomic_write(manifest_path, data)

    return {
        'strategy_id':    strategy_id,
        'before_regimes': before,
        'after_regimes':  canonical,
        'audited_at':     datetime.now(timezone.utc).isoformat(),
    }


def list_strategies(manifest_path: Path | None = None) -> list[dict]:
    manifest_path = manifest_path or DEFAULT_MANIFEST
    data = json.loads(manifest_path.read_text())
    strategies = data.get('strategies', {}) or {}
    out = []
    for sid, record in strategies.items():
        out.append({
            'strategy_id':      sid,
            'eligible_regimes': record.get('eligible_regimes'),  # None = no field
        })
    return out


def recent_audit(limit: int = 20) -> list[dict]:
    return _query_audit(limit)


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument('--list', action='store_true')
    sub.add_argument('--set', nargs='+', metavar='STRATEGY REGIME [REGIME ...]')
    sub.add_argument('--audit', action='store_true')
    p.add_argument('--actor', default='cli')
    p.add_argument('--reason', default='')
    p.add_argument('--source', default='')
    p.add_argument('--limit', type=int, default=20)
    args = p.parse_args()

    if args.list:
        for row in list_strategies():
            print(f"{row['strategy_id']}: {row['eligible_regimes']}")
        return 0
    if args.audit:
        for row in recent_audit(limit=args.limit):
            print(f"{row['changed_at']} {row['actor']:>16}  "
                  f"{row['strategy_id']}: {row['before_regimes']} -> {row['after_regimes']}  "
                  f"({row.get('reason') or ''})")
        return 0
    if args.set:
        if len(args.set) < 2:
            print('--set requires STRATEGY REGIME [REGIME ...]', file=sys.stderr)
            return 2
        strategy, *regimes = args.set
        result = set_eligibility(
            strategy_id=strategy, new_regimes=regimes,
            actor=args.actor, reason=args.reason, source=args.source,
        )
        print(json.dumps(result, indent=2))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
