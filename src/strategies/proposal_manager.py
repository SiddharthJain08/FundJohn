#!/usr/bin/env python3
"""Operator approval queue for Mastermind-proposed per-(strategy, regime)
parameter changes.

Lifecycle:
  Mastermind comprehensive-review -> insert_proposal() with status='pending'
  Saturday auto-apply (2026-07-14)-> auto_apply_batch(): confidence > 0.8 is
                                     auto-approved (source='auto-approval');
                                     everything else -> status='noted' (kept
                                     visible on the dashboard; superseded by
                                     next Saturday's fresh proposal = the
                                     re-evaluation loop)
  Operator (dashboard/CLI)        -> approve() / reject() / modify()
                                     ('noted' rows stay operator-decidable)
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
    """Mark all still-open (pending OR noted) proposals for (strategy, regime)
    as superseded. Called by Mastermind before inserting a new proposal for the
    same pair — this is how low-confidence 'noted' items get re-evaluated: the
    next weekly review re-proposes (or drops) them with fresh evidence.
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
                   AND status IN ('pending', 'noted')
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
    # 'noted' (low-confidence, auto-parked 2026-07-14) stays operator-decidable
    # from the dashboard until superseded by the next weekly review.
    if row[3] not in ('pending', 'noted'):
        raise ValueError(f'proposal {proposal_id} is not decidable (status={row[3]!r})')
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


def _current_size_scalar(strategy_id: str, regime_state: str):
    """Indirection seam: returns the current size_scalar from
    strategy_regime_params (or None if no row). Used by auto_approve for
    the bounded-delta gate."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT size_scalar
                  FROM strategy_regime_params
                 WHERE strategy_id = %s AND regime_state = %s
            """, (strategy_id, regime_state))
            row = cur.fetchone()
    return row[0] if row else None


def auto_approve(*, proposal_id: int) -> dict:
    """Auto-approve a pending proposal if it meets all rails. No-op otherwise.

    Env-gated by OPENCLAW_PROPOSAL_AUTOAPPROVE=1 (default OFF). When enabled,
    bounded by:
      OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE (default 0.85)
      OPENCLAW_PROPOSAL_AUTOAPPROVE_MAX_SIZE_DELTA (default 0.20)
      OPENCLAW_PROPOSAL_AUTOAPPROVE_MAX_STOP_DELTA (default 0.01)

    Returns one of:
      {'status': 'approved', ...}        — auto-approved
      {'status': 'skipped', 'reason': ...} — failed a rail OR feature disabled
    Raises:
      KeyError if proposal_id doesn't exist.
      ValueError if proposal not pending.
    """
    enabled = os.environ.get('OPENCLAW_PROPOSAL_AUTOAPPROVE') == '1'
    if not enabled:
        return {'id': proposal_id, 'status': 'skipped',
                'reason': 'auto-approval feature disabled (OPENCLAW_PROPOSAL_AUTOAPPROVE != 1)'}

    min_conf  = float(os.environ.get('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.85'))
    max_size  = float(os.environ.get('OPENCLAW_PROPOSAL_AUTOAPPROVE_MAX_SIZE_DELTA', '0.20'))
    max_stop  = float(os.environ.get('OPENCLAW_PROPOSAL_AUTOAPPROVE_MAX_STOP_DELTA', '0.01'))

    # Load proposal without locking (peek). The actual approve below re-locks.
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, strategy_id, regime_state, status,
                       proposed_eligible, proposed_size_scalar,
                       proposed_stop_pct, proposed_target_pct,
                       proposed_max_hold_days,
                       confidence, reasoning, memo_id
                  FROM strategy_regime_param_proposals
                 WHERE id = %s
            """, (proposal_id,))
            row = cur.fetchone()
    if row is None:
        raise KeyError(f'proposal {proposal_id} not found')
    prop = dict(zip(_PENDING_COLS, row))
    if prop['status'] != 'pending':
        raise ValueError(f'proposal {proposal_id} is not pending (status={prop["status"]!r})')

    # Rail 1: confidence
    conf = prop['confidence']
    if conf is None or float(conf) < min_conf:
        return {'id': proposal_id, 'status': 'skipped',
                'reason': f'confidence {conf} below threshold {min_conf}'}

    # Rail 2: size_scalar delta (only checked if proposing a size change)
    if prop['proposed_size_scalar'] is not None:
        current_size = _current_size_scalar(prop['strategy_id'], prop['regime_state'])
        current_size_f = float(current_size) if current_size is not None else 0.0
        proposed_size_f = float(prop['proposed_size_scalar'])
        if abs(proposed_size_f - current_size_f) > max_size:
            return {'id': proposal_id, 'status': 'skipped',
                    'reason': f'size delta |{proposed_size_f}-{current_size_f}| > {max_size}'}

    # Rail 3: stop_pct delta (only checked if proposing a stop change)
    if prop['proposed_stop_pct'] is not None:
        # Without a current_stop snapshot we treat the absolute proposed stop
        # as the delta (worst-case bound). Conservative.
        proposed_stop_f = abs(float(prop['proposed_stop_pct']))
        if proposed_stop_f > max_stop:
            return {'id': proposal_id, 'status': 'skipped',
                    'reason': f'stop_pct |{proposed_stop_f}| > {max_stop}'}

    # All rails passed → route through the normal approve path. Use an
    # actor tag that clearly identifies this as auto-approval for audit.
    auto_actor = 'auto-approval'
    auto_reason = (
        f'auto-approved: confidence={conf:.2f} >= {min_conf}, '
        f'rails (size_delta<={max_size}, stop<={max_stop}) all passed'
    )
    return _decide(proposal_id=proposal_id, actor=auto_actor,
                    reason=auto_reason, source='auto-approval',
                    overrides=None, terminal_status='approved')


def _mark_noted(proposal_id: int, reason: str) -> None:
    """Park a pending proposal as 'noted' (low confidence / rail-skipped).
    Noted rows stay visible on the dashboard Strategy Adjustments tab, remain
    operator-decidable, and are superseded by next Saturday's fresh proposal."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE strategy_regime_param_proposals
                   SET status = 'noted',
                       decided_at = NOW(),
                       decided_by = 'saturday_auto',
                       decision_reason = %s
                 WHERE id = %s AND status = 'pending'
            """, (reason, proposal_id))
        conn.commit()


def auto_apply_batch(*, threshold: Optional[float] = None, limit: int = 500,
                     log=logger.info) -> dict:
    """Saturday full-auto pass over ALL pending proposals (2026-07-14 operator
    directive): confidence strictly > threshold (default 0.8, env
    OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE) routes through auto_approve()
    (same set_params path as a dashboard click, source='auto-approval');
    everything else — low/missing confidence or a rail skip inside
    auto_approve — is parked as 'noted' with the reason recorded.

    Requires OPENCLAW_PROPOSAL_AUTOAPPROVE=1 (same master gate as
    auto_approve); returns a summary dict either way.
    """
    if os.environ.get('OPENCLAW_PROPOSAL_AUTOAPPROVE') != '1':
        log('[auto-apply] OPENCLAW_PROPOSAL_AUTOAPPROVE != 1 — skipping (no-op).')
        return {'skipped': True, 'approved': 0, 'noted': 0, 'errors': 0}
    thr = threshold if threshold is not None else float(
        os.environ.get('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.8'))
    approved = noted = errors = 0
    for prop in list_proposals(status='pending', limit=limit):
        pid = prop['id']
        conf = prop['confidence']
        try:
            if conf is None or float(conf) <= thr:
                _mark_noted(pid, f'confidence {conf} <= {thr} — noted for '
                                 f're-evaluation by next weekly review')
                noted += 1
                continue
            result = auto_approve(proposal_id=pid)
            if result.get('status') == 'approved':
                approved += 1
                log(f"[auto-apply] #{pid} {prop['strategy_id']}/{prop['regime_state']} "
                    f'approved (confidence {float(conf):.2f})')
            else:
                _mark_noted(pid, f"auto-apply rail skip: {result.get('reason', '?')}")
                noted += 1
                log(f"[auto-apply] #{pid} {prop['strategy_id']}/{prop['regime_state']} "
                    f"noted ({result.get('reason', '?')})")
        except Exception as e:
            errors += 1
            log(f'[auto-apply] #{pid} failed: {e}')
    summary = {'skipped': False, 'approved': approved, 'noted': noted,
               'errors': errors, 'threshold': thr}
    log(f'[auto-apply] done: {approved} approved, {noted} noted, {errors} errors '
        f'(threshold {thr}, strict >)')
    return summary


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
    sub.add_argument('--auto-apply-batch', action='store_true',
                     help='Saturday full-auto: approve pending proposals with '
                          'confidence strictly > threshold; note the rest.')
    p.add_argument('--threshold', type=float, default=None,
                   help='confidence threshold for --auto-apply-batch '
                        '(default env OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE or 0.8)')
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

    if args.auto_apply_batch:
        result = auto_apply_batch(threshold=args.threshold, log=print)
        print(json.dumps(result, indent=2, default=str))
        return 0 if not result.get('errors') else 1
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
