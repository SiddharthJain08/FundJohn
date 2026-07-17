#!/usr/bin/env python3
"""Confidence-calibration tracking for Mastermind proposals.

For each approved/modified/rejected proposal, computes live performance
in the (window_days BEFORE decision) vs (window_days AFTER decision)
window for the same (strategy, regime). Persists to
`mastermind_proposal_outcomes`. Reports Brier score + confidence-bucket
match rates.

Spec: docs/archive/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2d-design.md
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Optional

DEFAULT_WINDOW_DAYS = 30
BUCKETS = (
    (0.0, 0.2, '[0.0, 0.2]'),
    (0.2, 0.4, '[0.2, 0.4]'),
    (0.4, 0.6, '[0.4, 0.6]'),
    (0.6, 0.8, '[0.6, 0.8]'),
    (0.8, 1.001, '[0.8, 1.0]'),
)


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _live_sharpe_for_window(strategy_id: str, regime_state: str,
                              window_start, window_end) -> Optional[float]:
    """Annualized Sharpe over closed trades in [start, end)."""
    sql = """
        SELECT sp.realized_pnl_pct::float
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE es.strategy_id = %s
           AND es.regime_state = %s
           AND sp.realized_pnl_pct IS NOT NULL
           AND sp.closed_at >= %s
           AND sp.closed_at < %s
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (strategy_id, regime_state, window_start, window_end))
            pnls = [float(r[0]) for r in cur.fetchall()]
    if len(pnls) < 2:
        return None
    mean = sum(pnls) / len(pnls)
    var = sum((x - mean) ** 2 for x in pnls) / (len(pnls) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(252)


def _direction_match(*, proposal: dict, live_sharpe_pre: Optional[float],
                       live_sharpe_post: Optional[float]) -> bool:
    """Did live behavior change in the direction the proposal predicted?

    Heuristics:
    - size_up (proposed_size > current_size): post Sharpe should be >= pre
    - size_down (proposed_size < current_size): post Sharpe should be >= pre
      (less drag from a worse trade)
    - eligibility True: post Sharpe should be > pre (new regime adds alpha)
    - eligibility False: pre was bad (Sharpe < 0) — making ineligible is
      a "match" by removing drag
    """
    pre = live_sharpe_pre if live_sharpe_pre is not None else 0.0
    post = live_sharpe_post if live_sharpe_post is not None else 0.0
    if proposal.get('proposed_eligible') is False:
        # Making ineligible — match if the strategy was bleeding pre-decision.
        return pre < 0
    if proposal.get('proposed_eligible') is True:
        # Expanding eligibility — match if post Sharpe is positive.
        return post > 0
    proposed_size = proposal.get('proposed_size_scalar')
    current_size = proposal.get('current_size_scalar')
    if proposed_size is not None and current_size is not None:
        # Any size change: match if post >= pre (loosely)
        return post >= pre
    # Pure stop/target/max-hold change: no direction we can test simply.
    return post >= pre


def _brier_score(observations: list[dict]) -> float:
    """Brier = mean( (confidence - outcome)^2 ) for observations with
    confidence + direction_match."""
    valid = [o for o in observations
             if o.get('confidence') is not None
             and o.get('direction_match') is not None]
    if not valid:
        return float('nan')
    total = 0.0
    for o in valid:
        c = float(o['confidence'])
        y = 1.0 if o['direction_match'] else 0.0
        total += (c - y) ** 2
    return total / len(valid)


def _bucket_aggregates(observations: list[dict]) -> list[dict]:
    out = []
    for lo, hi, label in BUCKETS:
        bucket_obs = [o for o in observations
                      if o.get('confidence') is not None
                      and lo <= float(o['confidence']) < hi
                      and o.get('direction_match') is not None]
        matched = sum(1 for o in bucket_obs if o['direction_match'])
        out.append({
            'range':       label,
            'count':       len(bucket_obs),
            'matched':     matched,
            'match_rate':  matched / len(bucket_obs) if bucket_obs else None,
        })
    return out


def compute_outcome(proposal_id: int, window_days: int = DEFAULT_WINDOW_DAYS) -> Optional[dict]:
    """Compute one proposal's outcome. Persists; returns the row dict."""
    from datetime import timedelta
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, strategy_id, regime_state, status,
                       proposed_eligible, proposed_size_scalar,
                       confidence, decided_at,
                       (SELECT size_scalar FROM strategy_regime_params s
                          WHERE s.strategy_id = p.strategy_id
                            AND s.regime_state = p.regime_state) AS current_size
                  FROM strategy_regime_param_proposals p
                 WHERE p.id = %s
            """, (proposal_id,))
            row = cur.fetchone()
    if row is None:
        return None
    (pid, sid, regime, status, prop_elig, prop_size, conf, decided_at, current_size) = row
    if decided_at is None or status not in ('approved', 'modified', 'rejected'):
        return None
    window_start_pre  = decided_at - timedelta(days=window_days)
    window_start_post = decided_at
    window_end_post   = decided_at + timedelta(days=window_days)
    pre  = _live_sharpe_for_window(sid, regime, window_start_pre, window_start_post)
    post = _live_sharpe_for_window(sid, regime, window_start_post, window_end_post)
    direction = _direction_match(
        proposal={'proposed_eligible': prop_elig,
                  'proposed_size_scalar': float(prop_size) if prop_size is not None else None,
                  'current_size_scalar':  float(current_size) if current_size is not None else None},
        live_sharpe_pre=pre, live_sharpe_post=post,
    )
    pnl_delta = ((post or 0) - (pre or 0))
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mastermind_proposal_outcomes
                    (proposal_id, outcome_window_days, decided_at,
                     decision_status, confidence,
                     live_sharpe_pre, live_sharpe_post, live_pnl_delta,
                     direction_match)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (proposal_id) DO UPDATE
                   SET outcome_window_days = EXCLUDED.outcome_window_days,
                       live_sharpe_pre     = EXCLUDED.live_sharpe_pre,
                       live_sharpe_post    = EXCLUDED.live_sharpe_post,
                       live_pnl_delta      = EXCLUDED.live_pnl_delta,
                       direction_match     = EXCLUDED.direction_match,
                       computed_at         = NOW()
            """, (pid, window_days, decided_at, status, conf,
                  pre, post, pnl_delta, direction))
        conn.commit()
    return {
        'proposal_id': pid, 'window_days': window_days,
        'live_sharpe_pre': pre, 'live_sharpe_post': post,
        'direction_match': direction, 'confidence': float(conf) if conf is not None else None,
    }


def backfill_outcomes(since_days: int = 90,
                      window_days: int = DEFAULT_WINDOW_DAYS) -> int:
    """Compute outcomes for every decided proposal whose decision is at
    least `window_days` ago (so we have a full post-window of data)."""
    from datetime import datetime, timezone, timedelta
    cutoff_recent = datetime.now(timezone.utc) - timedelta(days=window_days)
    cutoff_old    = datetime.now(timezone.utc) - timedelta(days=since_days)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id
                  FROM strategy_regime_param_proposals
                 WHERE status IN ('approved', 'modified', 'rejected')
                   AND decided_at IS NOT NULL
                   AND decided_at <= %s
                   AND decided_at >= %s
            """, (cutoff_recent, cutoff_old))
            ids = [r[0] for r in cur.fetchall()]
    n = 0
    for pid in ids:
        if compute_outcome(pid, window_days=window_days) is not None:
            n += 1
    return n


def calibration_report() -> dict:
    """Pull all outcomes; compute Brier + bucket aggregates."""
    sql = """
        SELECT confidence, direction_match, decision_status
          FROM mastermind_proposal_outcomes
         WHERE confidence IS NOT NULL
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            obs = [{'confidence': float(r[0]) if r[0] is not None else None,
                    'direction_match': r[1],
                    'decision_status': r[2]} for r in cur.fetchall()]
    brier = _brier_score(obs)
    # NaN → None for JSON cross-compat (JS strict JSON.parse rejects NaN literal).
    if isinstance(brier, float) and brier != brier:
        brier = None
    return {
        'total_observations': len(obs),
        'brier_score':        brier,
        'buckets':            _bucket_aggregates(obs),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--backfill', type=int, default=None,
                    help='Backfill outcomes for proposals decided in last N days')
    p.add_argument('--window-days', type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument('--report', action='store_true')
    args = p.parse_args()
    if args.backfill is not None:
        n = backfill_outcomes(since_days=args.backfill, window_days=args.window_days)
        print(f'backfilled {n} outcome(s)')
    if args.report:
        print(json.dumps(calibration_report(), indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
