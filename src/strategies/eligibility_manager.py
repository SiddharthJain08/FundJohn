#!/usr/bin/env python3
"""DB-backed operator interface for per-(strategy, regime) params.

Source of truth: strategy_regime_params table. Every write goes through
set_params() which:
  1. Validates regime + ensures >=1 field specified
  2. Opens a transaction with SELECT ... FOR UPDATE on the existing row
  3. Inserts an audit row to strategy_regime_param_changes
  4. Upserts the params row
  5. Commits, then invalidates the resolver cache

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2a-design.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

# Match the resolver's path resolution.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _invalidate_cache(strategy_id: str, regime_state: str) -> None:
    """Best-effort: invalidate the same-process resolver cache. Cross-process
    caches expire on TTL (30s)."""
    try:
        from execution.regime_param_resolver import invalidate as _inv
        _inv(strategy_id, regime_state)
    except Exception as exc:
        logger.debug('cache invalidate skipped: %s', exc)


def _row_to_json(row) -> Optional[str]:
    if row is None:
        return None
    keys = ('strategy_id', 'regime_state', 'eligible',
            'size_scalar', 'stop_pct', 'target_pct', 'max_hold_days')
    d = dict(zip(keys, row))
    for k in ('size_scalar', 'stop_pct', 'target_pct'):
        if d[k] is not None:
            d[k] = float(d[k])
    return json.dumps(d)


def set_params(*,
               strategy_id: str,
               regime_state: str,
               eligible: Optional[bool] = None,
               size_scalar: Optional[float] = None,
               stop_pct: Optional[float] = None,
               target_pct: Optional[float] = None,
               max_hold_days: Optional[int] = None,
               actor: str,
               reason: str = '',
               source: str = 'cli') -> dict:
    """Upsert one (strategy, regime) row. None args mean 'keep existing'.

    Raises:
        ValueError: invalid regime or no fields specified.
    """
    if regime_state not in CANONICAL_REGIMES:
        raise ValueError(f'invalid regime {regime_state!r}; must be one of {CANONICAL_REGIMES}')

    if all(v is None for v in (eligible, size_scalar, stop_pct,
                                target_pct, max_hold_days)):
        raise ValueError('at least one of eligible/size_scalar/stop_pct/'
                         'target_pct/max_hold_days must be specified')

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, regime_state, eligible,
                       size_scalar, stop_pct, target_pct, max_hold_days
                  FROM strategy_regime_params
                 WHERE strategy_id = %s AND regime_state = %s
                 FOR UPDATE
            """, (strategy_id, regime_state))
            before = cur.fetchone()

            if before is None:
                merged_eligible      = True if eligible is None else eligible
                merged_size_scalar   = size_scalar
                merged_stop_pct      = stop_pct
                merged_target_pct    = target_pct
                merged_max_hold_days = max_hold_days
            else:
                # before = (sid, regime, eligible, size, stop, target, max_hold)
                merged_eligible      = before[2] if eligible is None else eligible
                merged_size_scalar   = before[3] if size_scalar is None else size_scalar
                merged_stop_pct      = before[4] if stop_pct is None else stop_pct
                merged_target_pct    = before[5] if target_pct is None else target_pct
                merged_max_hold_days = before[6] if max_hold_days is None else max_hold_days

            after_row = (strategy_id, regime_state, merged_eligible,
                         merged_size_scalar, merged_stop_pct,
                         merged_target_pct, merged_max_hold_days)

            cur.execute("""
                INSERT INTO strategy_regime_param_changes
                    (actor, strategy_id, regime_state,
                     before_row, after_row, reason, source)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            """, (actor, strategy_id, regime_state,
                  _row_to_json(before), _row_to_json(after_row),
                  reason, source))

            cur.execute("""
                INSERT INTO strategy_regime_params
                    (strategy_id, regime_state, eligible,
                     size_scalar, stop_pct, target_pct, max_hold_days, set_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (strategy_id, regime_state) DO UPDATE
                   SET eligible      = EXCLUDED.eligible,
                       size_scalar   = EXCLUDED.size_scalar,
                       stop_pct      = EXCLUDED.stop_pct,
                       target_pct    = EXCLUDED.target_pct,
                       max_hold_days = EXCLUDED.max_hold_days,
                       set_at        = NOW(),
                       set_by        = EXCLUDED.set_by
            """, (strategy_id, regime_state, merged_eligible,
                  merged_size_scalar, merged_stop_pct, merged_target_pct,
                  merged_max_hold_days, actor))
        conn.commit()

    _invalidate_cache(strategy_id, regime_state)

    return {
        'strategy_id':  strategy_id,
        'regime_state': regime_state,
        'before':       None if before is None else dict(zip(
            ('strategy_id', 'regime_state', 'eligible', 'size_scalar',
             'stop_pct', 'target_pct', 'max_hold_days'), before)),
        'after':        dict(zip(
            ('strategy_id', 'regime_state', 'eligible', 'size_scalar',
             'stop_pct', 'target_pct', 'max_hold_days'), after_row)),
        'audited_at':   datetime.now(timezone.utc).isoformat(),
    }


def list_rows() -> list[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, regime_state, eligible,
                       size_scalar, stop_pct, target_pct, max_hold_days
                  FROM strategy_regime_params
                 ORDER BY strategy_id, regime_state
            """)
            cols = ('strategy_id', 'regime_state', 'eligible',
                    'size_scalar', 'stop_pct', 'target_pct', 'max_hold_days')
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def recent_audit(limit: int = 25) -> list[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT changed_at, actor, strategy_id, regime_state,
                       before_row, after_row, reason, source
                  FROM strategy_regime_param_changes
                 ORDER BY changed_at DESC
                 LIMIT %s
            """, (limit,))
            cols = ('changed_at', 'actor', 'strategy_id', 'regime_state',
                    'before_row', 'after_row', 'reason', 'source')
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument('--list', action='store_true')
    sub.add_argument('--set', nargs=2, metavar=('STRATEGY', 'REGIME'))
    sub.add_argument('--audit', action='store_true')
    p.add_argument('--eligible', dest='eligible_flag',
                    action='store_const', const=True, default=None)
    p.add_argument('--ineligible', dest='eligible_flag',
                    action='store_const', const=False)
    p.add_argument('--size', type=float, default=None)
    p.add_argument('--stop', type=float, default=None)
    p.add_argument('--target', type=float, default=None)
    p.add_argument('--max-hold', type=int, default=None)
    p.add_argument('--actor', default='cli')
    p.add_argument('--reason', default='')
    p.add_argument('--source', default='cli')
    p.add_argument('--limit', type=int, default=25)
    args = p.parse_args()

    if args.list:
        for r in list_rows():
            print(f"{r['strategy_id']:<40} {r['regime_state']:<14} "
                  f"eligible={r['eligible']!s:<5} "
                  f"size={r['size_scalar']} stop={r['stop_pct']} "
                  f"target={r['target_pct']} maxhold={r['max_hold_days']}")
        return 0
    if args.audit:
        for r in recent_audit(limit=args.limit):
            print(f"{r['changed_at']} {r['actor']:>16}  "
                  f"{r['strategy_id']}/{r['regime_state']}  "
                  f"src={r['source']} reason={r['reason'] or ''}")
        return 0
    if args.set:
        strategy, regime = args.set
        result = set_params(strategy_id=strategy, regime_state=regime,
                             eligible=args.eligible_flag,
                             size_scalar=args.size,
                             stop_pct=args.stop,
                             target_pct=args.target,
                             max_hold_days=args.max_hold,
                             actor=args.actor, reason=args.reason,
                             source=args.source)
        print(json.dumps(result, indent=2, default=str))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
