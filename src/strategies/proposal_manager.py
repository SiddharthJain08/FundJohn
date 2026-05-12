#!/usr/bin/env python3
"""Operator approval queue for Mastermind-proposed per-(strategy, regime)
parameter changes.

Lifecycle:
  Mastermind comprehensive-review -> insert_proposal() with status='pending'
  Operator (dashboard/CLI)        -> approve() / reject() / modify()
  Approve/modify path             -> calls eligibility_manager.set_params,
                                     captures applied_row, marks status

Concurrency:
- `approve` / `reject` / `modify` open a transaction and `SELECT FOR UPDATE`
  the proposal row; second arriver sees status != 'pending' and errors.
- The downstream `eligibility_manager.set_params` uses its own SELECT FOR
  UPDATE on `strategy_regime_params` for the same reason.

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2b-design.md
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


def _set_params_via_manager(*, strategy_id, regime_state, eligible,
                             size_scalar, stop_pct, target_pct, max_hold_days,
                             actor, reason, source) -> dict:
    """Indirection seam so tests can stub the eligibility_manager call."""
    from strategies.eligibility_manager import set_params
    return set_params(
        strategy_id=strategy_id, regime_state=regime_state,
        eligible=eligible, size_scalar=size_scalar,
        stop_pct=stop_pct, target_pct=target_pct,
        max_hold_days=max_hold_days,
        actor=actor, reason=reason, source=source,
    )


def _decimal_to_jsonable(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


def _to_jsonb(d) -> Optional[str]:
    if d is None:
        return None
    cleaned = {}
    for k, v in d.items():
        if hasattr(v, '__float__') and not isinstance(v, (int, bool)):
            cleaned[k] = _decimal_to_jsonable(v)
        else:
            cleaned[k] = v
    return json.dumps(cleaned)


def insert_proposal(*, proposer: str, strategy_id: str, regime_state: str,
                     current_row: Optional[dict],
                     proposed_eligible: Optional[bool],
                     proposed_size_scalar: Optional[float],
                     proposed_stop_pct: Optional[float],
                     proposed_target_pct: Optional[float],
                     proposed_max_hold_days: Optional[int],
                     confidence: Optional[float],
                     reasoning: str,
                     memo_id) -> int:
    """Insert a new pending proposal. Returns the new proposal id."""
    if regime_state not in CANONICAL_REGIMES:
        raise ValueError(f'invalid regime {regime_state!r}')
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO strategy_regime_param_proposals
                    (proposer, strategy_id, regime_state, current_row,
                     proposed_eligible, proposed_size_scalar,
                     proposed_stop_pct, proposed_target_pct,
                     proposed_max_hold_days,
                     confidence, reasoning, memo_id, status)
                VALUES (%s, %s, %s, %s::jsonb,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, 'pending')
                RETURNING id
            """, (proposer, strategy_id, regime_state, _to_jsonb(current_row),
                  proposed_eligible, proposed_size_scalar,
                  proposed_stop_pct, proposed_target_pct,
                  proposed_max_hold_days,
                  confidence, reasoning, memo_id))
            row = cur.fetchone()
        conn.commit()
    return int(row[0])


def supersede_pending(strategy_id: str, regime_state: str,
                      new_proposer: str) -> int:
    """Mark all still-pending proposals for (strategy, regime) as superseded.
    Called by Mastermind before inserting a new proposal for the same pair.
    Returns the number of rows superseded."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE strategy_regime_param_proposals
                   SET status = 'superseded',
                       decided_at = NOW(),
                       decided_by = %s,
                       decision_reason = 'auto-superseded by newer proposal'
                 WHERE strategy_id = %s AND regime_state = %s
                   AND status = 'pending'
            """, (new_proposer, strategy_id, regime_state))
            n = cur.rowcount
        conn.commit()
    return int(n)


_PENDING_COLS = ('id', 'strategy_id', 'regime_state', 'status',
                  'proposed_eligible', 'proposed_size_scalar',
                  'proposed_stop_pct', 'proposed_target_pct',
                  'proposed_max_hold_days',
                  'confidence', 'reasoning', 'memo_id')


def _lock_for_decision(cur, proposal_id: int):
    cur.execute("""
        SELECT id, strategy_id, regime_state, status,
               proposed_eligible, proposed_size_scalar,
               proposed_stop_pct, proposed_target_pct,
               proposed_max_hold_days,
               confidence, reasoning, memo_id
          FROM strategy_regime_param_proposals
         WHERE id = %s
         FOR UPDATE
    """, (proposal_id,))
    row = cur.fetchone()
    if row is None:
        raise KeyError(f'proposal {proposal_id} not found')
    if row[3] != 'pending':
        raise ValueError(f'proposal {proposal_id} is not pending (status={row[3]!r})')
    return dict(zip(_PENDING_COLS, row))


def approve(*, proposal_id: int, actor: str, reason: str = '',
            source: str = 'dashboard') -> dict:
    return _decide(proposal_id=proposal_id, actor=actor, reason=reason,
                   source=source, overrides=None, terminal_status='approved')


def modify(*, proposal_id: int, actor: str, overrides: dict, reason: str,
           source: str = 'dashboard') -> dict:
    return _decide(proposal_id=proposal_id, actor=actor, reason=reason,
                   source=source, overrides=overrides, terminal_status='modified')


def reject(*, proposal_id: int, actor: str, reason: str = '',
           source: str = 'dashboard') -> dict:
    with _connect() as conn:
        with conn.cursor() as cur:
            prop = _lock_for_decision(cur, proposal_id)
            cur.execute("""
                UPDATE strategy_regime_param_proposals
                   SET status = 'rejected',
                       decided_at = NOW(),
                       decided_by = %s,
                       decision_reason = %s
                 WHERE id = %s
            """, (actor, reason, proposal_id))
        conn.commit()
    return {
        'id':         proposal_id,
        'strategy_id': prop['strategy_id'],
        'regime_state': prop['regime_state'],
        'status':     'rejected',
        'decided_at': datetime.now(timezone.utc).isoformat(),
        'decided_by': actor,
    }


def _decide(*, proposal_id, actor, reason, source, overrides, terminal_status):
    """Shared approve/modify path."""
    with _connect() as conn:
        with conn.cursor() as cur:
            prop = _lock_for_decision(cur, proposal_id)

            final_eligible      = prop['proposed_eligible']
            final_size_scalar   = prop['proposed_size_scalar']
            final_stop_pct      = prop['proposed_stop_pct']
            final_target_pct    = prop['proposed_target_pct']
            final_max_hold_days = prop['proposed_max_hold_days']
            if overrides:
                final_eligible      = overrides.get('eligible',       final_eligible)
                final_size_scalar   = overrides.get('size_scalar',    final_size_scalar)
                final_stop_pct      = overrides.get('stop_pct',       final_stop_pct)
                final_target_pct    = overrides.get('target_pct',     final_target_pct)
                final_max_hold_days = overrides.get('max_hold_days',  final_max_hold_days)

            sp = _set_params_via_manager(
                strategy_id=prop['strategy_id'],
                regime_state=prop['regime_state'],
                eligible=final_eligible,
                size_scalar=(float(final_size_scalar)
                             if final_size_scalar is not None else None),
                stop_pct=(float(final_stop_pct)
                          if final_stop_pct is not None else None),
                target_pct=(float(final_target_pct)
                            if final_target_pct is not None else None),
                max_hold_days=(int(final_max_hold_days)
                                if final_max_hold_days is not None else None),
                actor=actor,
                reason=f'[proposal #{proposal_id}] {reason}',
                source=source,
            )

            applied_row = sp.get('after') if isinstance(sp, dict) else None

            cur.execute("""
                UPDATE strategy_regime_param_proposals
                   SET status = %s,
                       decided_at = NOW(),
                       decided_by = %s,
                       decision_reason = %s,
                       applied_row = %s::jsonb
                 WHERE id = %s
            """, (terminal_status, actor, reason,
                  _to_jsonb(applied_row), proposal_id))
        conn.commit()
    return {
        'id':           proposal_id,
        'strategy_id':  prop['strategy_id'],
        'regime_state': prop['regime_state'],
        'status':       terminal_status,
        'applied_row':  applied_row,
        'decided_at':   datetime.now(timezone.utc).isoformat(),
        'decided_by':   actor,
    }


def list_proposals(*, status: str = 'pending', limit: int = 50,
                   strategy_id: Optional[str] = None) -> list[dict]:
    sql = """
        SELECT id, strategy_id, regime_state, status,
               proposed_eligible, proposed_size_scalar,
               proposed_stop_pct, proposed_target_pct,
               proposed_max_hold_days,
               confidence, reasoning, memo_id,
               proposed_at, decided_at, decided_by, decision_reason
          FROM strategy_regime_param_proposals
         WHERE status = %s
    """
    params: list = [status]
    if strategy_id:
        sql += ' AND strategy_id = %s'
        params.append(strategy_id)
    sql += ' ORDER BY proposed_at DESC LIMIT %s'
    params.append(limit)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument('--list', action='store_true')
    sub.add_argument('--approve', type=int, metavar='PROPOSAL_ID')
    sub.add_argument('--reject', type=int, metavar='PROPOSAL_ID')
    sub.add_argument('--modify', type=int, metavar='PROPOSAL_ID')
    p.add_argument('--status', default='pending')
    p.add_argument('--actor', default='cli')
    p.add_argument('--reason', default='')
    p.add_argument('--source', default='cli')
    p.add_argument('--size', type=float, default=None)
    p.add_argument('--stop', type=float, default=None)
    p.add_argument('--target', type=float, default=None)
    p.add_argument('--max-hold', type=int, default=None)
    p.add_argument('--eligible', dest='eligible_flag',
                    action='store_const', const=True, default=None)
    p.add_argument('--ineligible', dest='eligible_flag',
                    action='store_const', const=False)
    args = p.parse_args()

    if args.list:
        rows = list_proposals(status=args.status)
        for r in rows:
            print(f"#{r['id']:<4} {r['strategy_id']:<32} {r['regime_state']:<14} "
                  f"-> eligible={r['proposed_eligible']} size={r['proposed_size_scalar']} "
                  f"conf={r['confidence']}  {r['reasoning'] or ''}")
        return 0
    if args.approve:
        result = approve(proposal_id=args.approve, actor=args.actor,
                          reason=args.reason, source=args.source)
        print(json.dumps(result, indent=2, default=str))
        return 0
    if args.reject:
        result = reject(proposal_id=args.reject, actor=args.actor,
                         reason=args.reason, source=args.source)
        print(json.dumps(result, indent=2, default=str))
        return 0
    if args.modify:
        overrides = {}
        if args.eligible_flag is not None: overrides['eligible'] = args.eligible_flag
        if args.size is not None:          overrides['size_scalar'] = args.size
        if args.stop is not None:          overrides['stop_pct'] = args.stop
        if args.target is not None:        overrides['target_pct'] = args.target
        if args.max_hold is not None:      overrides['max_hold_days'] = args.max_hold
        if not overrides:
            print('--modify requires at least one of --eligible/--ineligible/--size/'
                  '--stop/--target/--max-hold', file=sys.stderr)
            return 2
        result = modify(proposal_id=args.modify, actor=args.actor,
                        overrides=overrides, reason=args.reason,
                        source=args.source)
        print(json.dumps(result, indent=2, default=str))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
