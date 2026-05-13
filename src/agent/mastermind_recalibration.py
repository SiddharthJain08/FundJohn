#!/usr/bin/env python3
"""Phase 2F — Mastermind prompt recalibration loop.

Reads the calibration report from Phase 2D, detects systematic confidence
bias per bucket, and emits operator-approval-gated `addenda` that get
prepended to the next Saturday comprehensive-review Opus prompt.

DORMANCY: until each bucket has ≥MIN_BUCKET_SAMPLES decided proposals
with ≥30d outcome windows, auto-emission returns INSUFFICIENT and inserts
nothing. As of 2026-05-13, mastermind_proposal_outcomes is empty; this
infrastructure is wiring + manual-mode escape hatch.

Operators can hand-author addenda at any time via `--add "TEXT" --rationale ...`;
those land as `active` immediately (source='operator:<decided_by>').

Spec: docs/superpowers/specs/2026-05-13-regime-blended-sizer-phase-2f-design.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

MIN_BUCKET_SAMPLES = 10
BIAS_DELTA_THRESHOLD = 0.15   # |match_rate - midpoint| > 0.15 → biased
MAX_ACTIVE_ADDENDA   = 5      # soft limit; warn beyond


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _bucket_midpoint(label: str) -> Optional[float]:
    """Extract midpoint from a label like '[0.0, 0.2]'. Returns 0.1, 0.3..."""
    label = label.strip()
    if not (label.startswith('[') and label.endswith(']')):
        return None
    try:
        lo, hi = (float(x) for x in label[1:-1].split(','))
        return (lo + hi) / 2.0
    except Exception:
        return None


# ---------- bias detection ---------- #

def detect_bias(report: Optional[dict] = None) -> list[dict]:
    """Scan calibration buckets for systematic over/underconfidence.

    Returns a list with one entry per BIASED bucket (skips INSUFFICIENT
    buckets and unbiased ones). Each entry:
      {bucket, count, match_rate, midpoint, delta, direction}
    direction ∈ {'overconfident', 'underconfident'}.

    Pass a precomputed report (e.g. test stub) or let it pull live.
    """
    if report is None:
        from metrics.mastermind_calibration import calibration_report
        report = calibration_report()
    out: list[dict] = []
    for b in (report or {}).get('buckets', []):
        count = int(b.get('count') or 0)
        match_rate = b.get('match_rate')
        label = b.get('range') or ''
        mid = _bucket_midpoint(label)
        if count < MIN_BUCKET_SAMPLES or match_rate is None or mid is None:
            continue
        delta = float(match_rate) - mid
        if abs(delta) <= BIAS_DELTA_THRESHOLD:
            continue
        out.append({
            'bucket':     label,
            'count':      count,
            'match_rate': float(match_rate),
            'midpoint':   mid,
            'delta':      delta,
            'direction':  'overconfident' if delta < 0 else 'underconfident',
        })
    return out


def generate_addendum(bias_entry: dict) -> str:
    """Templated, plain-text addendum. No template-rendering of user input."""
    rate_pct = round(bias_entry['match_rate'] * 100, 1)
    mid_pct = round(bias_entry['midpoint'] * 100, 1)
    direction = bias_entry['direction']
    if direction == 'overconfident':
        return (f"Recent calibration: your {bias_entry['bucket']} confidence "
                 f"proposals matched only {rate_pct}% of the time vs the "
                 f"{mid_pct}% expected from the midpoint. Discount confidence "
                 f"in this bucket and prefer narrower size/stop deltas until "
                 f"the match rate recovers.")
    return (f"Recent calibration: your {bias_entry['bucket']} confidence "
             f"proposals matched {rate_pct}% of the time vs the {mid_pct}% "
             f"expected from the midpoint — you are underconfident. Where "
             f"reasoning supports it, take stronger position-sizing recommendations "
             f"in this bucket.")


# ---------- DB operations ---------- #

def emit_auto_addenda(dry_run: bool = False,
                      report: Optional[dict] = None) -> dict:
    """Run detector and insert one pending addendum per biased bucket.

    If a pending or active auto-addendum already exists for the same bucket,
    the new one is inserted with `supersedes_id` set and the old auto-row
    transitions to 'superseded'. Operator-authored rows are never touched.

    Returns dict {emitted: [{id, bucket, ...}], dry_run, status}.
    """
    biases = detect_bias(report=report)
    if not biases:
        return {'status': 'NONE_BIASED', 'emitted': [], 'dry_run': dry_run}
    emitted: list[dict] = []
    with _connect() as conn:
        with conn.cursor() as cur:
            for bias in biases:
                text = generate_addendum(bias)
                cur.execute("""
                    SELECT id FROM mastermind_prompt_addenda
                     WHERE source LIKE 'auto:%%'
                       AND triggered_by->>'bucket' = %s
                       AND status IN ('pending', 'active')
                     ORDER BY created_at DESC LIMIT 1
                """, (bias['bucket'],))
                row = cur.fetchone()
                supersedes = row[0] if row else None
                if dry_run:
                    emitted.append({'bucket': bias['bucket'], 'text': text,
                                     'supersedes_id': supersedes, 'dry_run': True})
                    continue
                if supersedes:
                    cur.execute("""
                        UPDATE mastermind_prompt_addenda
                           SET status='superseded', decided_at=NOW(),
                               decided_by='auto:bias_detector',
                               decision_reason='replaced by newer auto-addendum'
                         WHERE id=%s
                    """, (supersedes,))
                cur.execute("""
                    INSERT INTO mastermind_prompt_addenda
                        (source, triggered_by, addendum_text, status, supersedes_id)
                    VALUES ('auto:bias_detector', %s::jsonb, %s, 'pending', %s)
                    RETURNING id
                """, (json.dumps(bias), text, supersedes))
                new_id = cur.fetchone()[0]
                emitted.append({'id': new_id, 'bucket': bias['bucket'],
                                 'supersedes_id': supersedes})
        if not dry_run:
            conn.commit()
    return {'status': 'OK', 'emitted': emitted, 'dry_run': dry_run}


def approve_addendum(addendum_id: int, decided_by: str,
                      reason: str = '') -> dict:
    return _decide(addendum_id, 'active', decided_by, reason)


def reject_addendum(addendum_id: int, decided_by: str,
                     reason: str = '') -> dict:
    return _decide(addendum_id, 'rejected', decided_by, reason)


def expire_addendum(addendum_id: int, decided_by: str,
                     reason: str = '') -> dict:
    return _decide(addendum_id, 'expired', decided_by, reason)


def _decide(addendum_id: int, new_status: str, decided_by: str,
             reason: str) -> dict:
    """Set status with FOR UPDATE lock + audit trail. new_status must be
    a legal transition from current status."""
    legal = {
        'pending':    {'active', 'rejected'},
        'active':     {'expired', 'superseded'},
        'expired':    set(),
        'rejected':   set(),
        'superseded': set(),
    }
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status FROM mastermind_prompt_addenda
                 WHERE id=%s FOR UPDATE
            """, (addendum_id,))
            row = cur.fetchone()
            if not row:
                return {'status': 'NOT_FOUND', 'id': addendum_id}
            current = row[0]
            if new_status not in legal.get(current, set()):
                return {'status': 'ILLEGAL_TRANSITION',
                        'id': addendum_id, 'current': current,
                        'requested': new_status}
            cur.execute("""
                UPDATE mastermind_prompt_addenda
                   SET status=%s, decided_at=NOW(), decided_by=%s,
                       decision_reason=%s,
                       valid_from=COALESCE(valid_from, CASE WHEN %s='active' THEN NOW() ELSE valid_from END)
                 WHERE id=%s
            """, (new_status, decided_by, reason, new_status, addendum_id))
        conn.commit()
    return {'status': 'OK', 'id': addendum_id, 'new_status': new_status}


def create_operator_addendum(text: str, rationale: str, decided_by: str,
                              valid_until: Optional[datetime] = None) -> int:
    """Operator-authored addendum: status='active' immediately, no
    bias-detector trigger. Returns the new row id."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mastermind_prompt_addenda
                    (source, addendum_text, rationale, status,
                     valid_from, valid_until, decided_at, decided_by,
                     decision_reason)
                VALUES (%s, %s, %s, 'active',
                        NOW(), %s, NOW(), %s,
                        'operator-authored')
                RETURNING id
            """, (f'operator:{decided_by}', text, rationale,
                  valid_until, decided_by))
            row_id = cur.fetchone()[0]
        conn.commit()
    return row_id


def get_active_addenda(now: Optional[datetime] = None) -> list[dict]:
    """Return all addenda where status='active' and (valid_until is NULL
    OR valid_until > now). Auto-flips past-due rows to 'expired' as a
    side effect.
    """
    now = now or datetime.now(timezone.utc)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE mastermind_prompt_addenda
                   SET status='expired', decided_at=NOW(),
                       decided_by='system:auto-expire',
                       decision_reason='past valid_until'
                 WHERE status='active'
                   AND valid_until IS NOT NULL
                   AND valid_until <= %s
            """, (now,))
            cur.execute("""
                SELECT id, source, addendum_text, rationale,
                       valid_from, valid_until, created_at
                  FROM mastermind_prompt_addenda
                 WHERE status='active'
                   AND (valid_from IS NULL OR valid_from <= %s)
                 ORDER BY created_at
            """, (now,))
            rows = cur.fetchall()
        conn.commit()
    return [{
        'id':             r[0],
        'source':         r[1],
        'addendum_text':  r[2],
        'rationale':      r[3],
        'valid_from':     r[4].isoformat() if r[4] else None,
        'valid_until':    r[5].isoformat() if r[5] else None,
        'created_at':     r[6].isoformat() if r[6] else None,
    } for r in rows]


def list_addenda(status: Optional[str] = None) -> list[dict]:
    sql = "SELECT id, status, source, addendum_text, created_at FROM mastermind_prompt_addenda"
    params: tuple = ()
    if status:
        sql += " WHERE status=%s"
        params = (status,)
    sql += " ORDER BY created_at DESC"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [{'id': r[0], 'status': r[1], 'source': r[2],
                      'text': r[3], 'created_at': r[4].isoformat() if r[4] else None}
                     for r in cur.fetchall()]


# ---------- CLI ---------- #

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--detect', action='store_true', help='Run bias detector; print biases (no insert)')
    p.add_argument('--emit', action='store_true', help='Run detector + insert pending addenda')
    p.add_argument('--dry-run', action='store_true', help='With --emit, print what would be inserted')
    p.add_argument('--list', metavar='STATUS', nargs='?', const='all',
                    help='List addenda, optionally filtered by status')
    p.add_argument('--list-active', action='store_true',
                    help='Print JSON {addenda: [...]} for active addenda (used by comprehensive_review.js)')
    p.add_argument('--approve', type=int, metavar='ID')
    p.add_argument('--reject',  type=int, metavar='ID')
    p.add_argument('--expire',  type=int, metavar='ID')
    p.add_argument('--decided-by', default=os.environ.get('USER', 'operator'))
    p.add_argument('--reason', default='')
    p.add_argument('--add', metavar='TEXT', help='Operator-authored addendum text')
    p.add_argument('--rationale', default='')
    p.add_argument('--valid-until', default=None,
                    help='ISO timestamp; addendum auto-expires after this')
    args = p.parse_args()

    if args.detect:
        biases = detect_bias()
        print(json.dumps(biases, indent=2))
        return 0
    if args.emit:
        result = emit_auto_addenda(dry_run=args.dry_run)
        print(json.dumps(result, indent=2, default=str))
        return 0
    if args.list_active:
        addenda = get_active_addenda()
        print(json.dumps({'addenda': addenda}, default=str))
        return 0
    if args.list is not None:
        rows = list_addenda(status=None if args.list == 'all' else args.list)
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if args.add:
        valid_until = None
        if args.valid_until:
            valid_until = datetime.fromisoformat(args.valid_until.replace('Z', '+00:00'))
        new_id = create_operator_addendum(args.add, args.rationale,
                                           decided_by=args.decided_by,
                                           valid_until=valid_until)
        print(json.dumps({'id': new_id, 'status': 'active'}))
        return 0
    if args.approve:
        print(json.dumps(approve_addendum(args.approve, args.decided_by, args.reason)))
        return 0
    if args.reject:
        print(json.dumps(reject_addendum(args.reject, args.decided_by, args.reason)))
        return 0
    if args.expire:
        print(json.dumps(expire_addendum(args.expire, args.decided_by, args.reason)))
        return 0
    p.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
