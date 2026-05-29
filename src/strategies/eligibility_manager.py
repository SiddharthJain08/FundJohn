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

# Sentinel marking "explicitly reset this column to SQL NULL".
# Passing None means "keep existing"; passing NULL_SENTINEL means "set to NULL".
# Phase 2C addition — needed so operators / proposal approvals can roll back a
# populated size_scalar/stop_pct/target_pct/max_hold_days to Phase 1 defaults.
NULL_SENTINEL = '__NULL__'


def _resolve_value(caller_value, existing_value):
    """Three-state resolver for nullable columns:
       caller_value is None        → keep existing
       caller_value is NULL_SENTINEL → reset to NULL
       otherwise                    → use caller value"""
    if caller_value is None:
        return existing_value
    if caller_value == NULL_SENTINEL:
        return None
    return caller_value

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
               source: str = 'cli',
               bt_sharpe_before: Optional[float] = None,
               bt_sharpe_after: Optional[float] = None,
               bt_n_trades: Optional[int] = None) -> dict:
    """Upsert one (strategy, regime) row. None args mean 'keep existing'.

    Raises:
        ValueError: invalid regime or no fields specified.
    """
    if regime_state not in CANONICAL_REGIMES:
        raise ValueError(f'invalid regime {regime_state!r}; must be one of {CANONICAL_REGIMES}')

    if all(v is None for v in (eligible, size_scalar, stop_pct,
                                target_pct, max_hold_days)):
        raise ValueError('at least one of eligible/size_scalar/stop_pct/'
                         'target_pct/max_hold_days must be specified '
                         "(use NULL_SENTINEL '__NULL__' to reset a column to NULL)")

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
                # eligible defaults True for first-set (NULL_SENTINEL not
                # meaningful for boolean column).
                merged_eligible      = True if eligible is None else eligible
                merged_size_scalar   = _resolve_value(size_scalar, None)
                merged_stop_pct      = _resolve_value(stop_pct, None)
                merged_target_pct    = _resolve_value(target_pct, None)
                merged_max_hold_days = _resolve_value(max_hold_days, None)
            else:
                # before = (sid, regime, eligible, size, stop, target, max_hold)
                merged_eligible      = before[2] if eligible is None else eligible
                merged_size_scalar   = _resolve_value(size_scalar,    before[3])
                merged_stop_pct      = _resolve_value(stop_pct,       before[4])
                merged_target_pct    = _resolve_value(target_pct,     before[5])
                merged_max_hold_days = _resolve_value(max_hold_days,  before[6])

            after_row = (strategy_id, regime_state, merged_eligible,
                         merged_size_scalar, merged_stop_pct,
                         merged_target_pct, merged_max_hold_days)

            cur.execute("""
                INSERT INTO strategy_regime_param_changes
                    (actor, strategy_id, regime_state,
                     before_row, after_row, reason, source,
                     bt_sharpe_before, bt_sharpe_after, bt_n_trades)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
            """, (actor, strategy_id, regime_state,
                  _row_to_json(before), _row_to_json(after_row),
                  reason, source,
                  bt_sharpe_before, bt_sharpe_after, bt_n_trades))

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


def recent_param_audit(limit: int = 25) -> list[dict]:
    """Audit trail for the DB-backed regime-params flow (set_params).
    Renamed from recent_audit on 2026-05-18 to disambiguate from the
    manifest-eligibility audit flow added below."""
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


# ── Manifest-eligibility flow (parallel to the DB-backed set_params above) ──
#
# Operator path for editing a strategy's eligible_regimes field in
# src/strategies/manifest.json. Atomic write (tempfile+rename) + audit
# row that gets inserted BEFORE the manifest is touched, so an audit
# failure leaves the manifest unmodified. Tests monkeypatch
# _insert_audit / _query_audit to avoid DB dependency.


def _insert_audit(*, strategy_id: str,
                  before_regimes: Optional[list[str]],
                  after_regimes: list[str],
                  actor: str,
                  reason: str = '',
                  source: str = '') -> None:
    """Persist one manifest-eligibility change for audit.

    Default impl writes to strategy_regime_param_changes with
    regime_state='ALL_MANIFEST' so the row coexists with per-regime
    param audits without colliding. Tests monkeypatch this entirely.
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO strategy_regime_param_changes
                    (actor, strategy_id, regime_state,
                     before_row, after_row, reason, source)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            """, (actor, strategy_id, 'ALL_MANIFEST',
                  json.dumps({'eligible_regimes': before_regimes}),
                  json.dumps({'eligible_regimes': after_regimes}),
                  reason, source))
        conn.commit()


def _query_audit(limit: int) -> list[dict]:
    """Return recent manifest-eligibility audit rows. Default impl
    pulls from strategy_regime_param_changes WHERE regime_state =
    'ALL_MANIFEST'. Tests monkeypatch."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT changed_at, actor, strategy_id,
                       before_row, after_row, reason, source
                  FROM strategy_regime_param_changes
                 WHERE regime_state = 'ALL_MANIFEST'
                 ORDER BY changed_at DESC
                 LIMIT %s
            """, (limit,))
            cols = ('changed_at', 'actor', 'strategy_id',
                    'before_row', 'after_row', 'reason', 'source')
            out = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                # Decode before_row / after_row JSONB into list-of-regimes
                for field, key in (('before_row', 'before_regimes'),
                                   ('after_row',  'after_regimes')):
                    v = d.get(field)
                    if isinstance(v, dict):
                        d[key] = v.get('eligible_regimes')
                    elif isinstance(v, str):
                        try:
                            d[key] = json.loads(v).get('eligible_regimes')
                        except (ValueError, AttributeError):
                            d[key] = None
                    else:
                        d[key] = None
                out.append(d)
            return out


def set_eligibility(*,
                    strategy_id: str,
                    new_regimes: list[str],
                    actor: str,
                    reason: str = '',
                    source: str = '',
                    manifest_path) -> None:
    """Update manifest.json's strategies[strategy_id].eligible_regimes
    to `new_regimes`. Validates, dedupes, canonicalises order, writes
    audit FIRST (so a DB failure leaves the manifest unchanged), then
    atomically rewrites the manifest via tempfile+rename.

    Raises:
        ValueError: invalid regime or empty list.
        KeyError:   strategy_id not in manifest.strategies.
    """
    if not new_regimes:
        raise ValueError('at least one regime required (cannot make a strategy '
                         'eligible in zero regimes — use a state transition instead)')
    invalid = [r for r in new_regimes if r not in CANONICAL_REGIMES]
    if invalid:
        raise ValueError(f'invalid regime(s) {invalid}; must be one of {CANONICAL_REGIMES}')
    # Dedup + canonical order (LOW_VOL, TRANSITIONING, HIGH_VOL, CRISIS).
    seen: set[str] = set()
    deduped: list[str] = []
    for r in CANONICAL_REGIMES:
        if r in new_regimes and r not in seen:
            seen.add(r)
            deduped.append(r)

    p = Path(manifest_path)
    manifest = json.loads(p.read_text())
    strategies = manifest.get('strategies') or {}
    if strategy_id not in strategies:
        raise KeyError(strategy_id)

    before_regimes = strategies[strategy_id].get('eligible_regimes')

    # Audit FIRST. If this raises, the manifest is unchanged.
    _insert_audit(strategy_id=strategy_id,
                  before_regimes=before_regimes,
                  after_regimes=deduped,
                  actor=actor, reason=reason, source=source)

    # Atomic update — write to tempfile, then rename.
    strategies[strategy_id]['eligible_regimes'] = deduped
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(manifest, indent=2) + '\n')
    tmp.replace(p)


def list_strategies(*, manifest_path) -> list[dict]:
    """Return [{strategy_id, eligible_regimes}, ...] from the manifest.
    Strategies without an `eligible_regimes` key get None as a
    backward-compat marker so callers can distinguish unset from empty."""
    p = Path(manifest_path)
    manifest = json.loads(p.read_text())
    strategies = manifest.get('strategies') or {}
    return [
        {'strategy_id':       sid,
         'eligible_regimes':  entry.get('eligible_regimes')}
        for sid, entry in strategies.items()
    ]


def recent_audit(limit: int = 25) -> list[dict]:
    """Manifest-eligibility audit trail. Delegates to _query_audit so
    tests can monkeypatch the underlying source."""
    return _query_audit(limit)


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
        for r in recent_param_audit(limit=args.limit):
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
