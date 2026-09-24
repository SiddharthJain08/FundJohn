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
                       live_sharpe_post: Optional[float]) -> Optional[bool]:
    """Did live behavior change in the direction the proposal predicted?

    Heuristics:
    - size_up (proposed_size > current_size): post Sharpe should be >= pre
    - size_down (proposed_size < current_size): post Sharpe should be >= pre
      (less drag from a worse trade)
    - eligibility True: post Sharpe should be > pre (new regime adds alpha)
    - eligibility False: pre was bad (Sharpe < 0) — making ineligible is
      a "match" by removing drag

    Returns None when the decisive window carries NO evidence (no closed
    trades ⇒ Sharpe None): an eligibility expansion needs the post window, a
    restriction needs the pre window, a size/stop change needs both. Scoring
    those as misses (the pre-2026-09-06 behaviour, which coerced None to 0.0)
    inflated the Brier score from 0.25 to 0.43 on 96 outcomes, 55 of which had
    no evidence in the window that mattered.
    """
    if proposal.get('proposed_eligible') is False:
        # Making ineligible — match if the strategy was bleeding pre-decision.
        if live_sharpe_pre is None:
            return None
        return live_sharpe_pre < 0
    if proposal.get('proposed_eligible') is True:
        # Expanding eligibility — match if post Sharpe is positive.
        if live_sharpe_post is None:
            return None
        return live_sharpe_post > 0
    if live_sharpe_pre is None or live_sharpe_post is None:
        return None
    # Any size / stop / target / max-hold change: match if post >= pre (loosely).
    return live_sharpe_post >= live_sharpe_pre


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


# ── D2: outcome-calibrated confidence + evidence cap (spec 2026-09-12 §4) ─────
# Evidence for these numbers: the 2026-09-06 recompute found the mastermind
# over-confident — Brier 0.254 on 41 resolved outcomes, hit rate 0.66 against a
# mean stated confidence of 0.75, and 0.56 (10/18) inside the >=0.8 bucket that
# auto-approval actually reads. calibrated_confidence turns that observation
# into the number the floor is compared against instead of the stated one.

MIN_BUCKET_N         = 8      # below this a bucket's rate is noise — pass raw through
EVIDENCE_WINDOW_DAYS = 30     # matches DEFAULT_WINDOW_DAYS, the outcome window
EVIDENCE_STALE_DAYS  = 45     # no closed trade in this long => one level down
EVIDENCE_LEVELS      = ('none', 'low', 'medium', 'high')
EVIDENCE_CAPS        = {'none': 0.35, 'low': 0.55, 'medium': 0.75, 'high': 1.0}


def bucket_midpoint(lo: float, hi: float) -> float:
    """Midpoint of a BUCKETS range. The top bucket's `hi` is 1.001 (an
    exclusive-upper trick so confidence == 1.0 lands somewhere), so clamp it
    back to 1.0 before averaging — otherwise the [0.8, 1.0] midpoint would be
    0.9005 and every top-bucket remap would carry a spurious deflation."""
    return (float(lo) + min(float(hi), 1.0)) / 2.0


def bucket_for(conf):
    """The (lo, hi, label) BUCKETS triple containing `conf`; None when conf is
    None or outside [0, 1]."""
    if conf is None:
        return None
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return None
    if c < 0.0 or c > 1.0:
        return None
    for lo, hi, label in BUCKETS:
        if lo <= c < hi:
            return (lo, hi, label)
    return None


def calibrated_confidence(raw, bucket_table, *, min_n: int = MIN_BUCKET_N):
    """raw x clip(match_rate(bucket) / bucket_midpoint, 0.5, 1.0), or raw when
    the bucket has fewer than `min_n` resolved observations.

    `bucket_table` is the list _bucket_aggregates / calibration_report()['buckets']
    returns: rows of {'range', 'count', 'matched', 'match_rate'}. NOTE the key is
    `match_rate` (per-bucket); `hit_rate` on the report is the GLOBAL figure.

    The ratio's upper clip is 1.0 by design: this may only deflate an
    over-confident stated number, never inflate an under-confident one — an
    auto-approval floor must not be crossed by a bonus.

    `raw` is clamped to [0.0, 1.0] before it is bucketed AND before the ratio
    is applied to it — a stray value outside that range (e.g. float drift
    upstream) must not pick a bucket outside BUCKETS' domain, nor let the
    returned value exceed the clamped raw. NaN fails the `x == x`
    self-equality check and maps to None rather than silently sorting into
    a bucket.

    This map is NOT monotone across a bucket boundary. Worked counter-example:
    suppose the [0.6, 0.8] bucket's clipped ratio is 1.0 (well- or
    under-calibrated) and the [0.8, 1.0] bucket's clipped ratio is 0.5 (badly
    over-confident). Then raw=0.79 (falls in [0.6, 0.8]) calibrates to
    0.79 * 1.0 = 0.79, but raw=0.80 (falls in [0.8, 1.0]) calibrates to
    0.80 * 0.5 = 0.40 — a one-cent rise in the stated confidence produces a
    0.39 DROP in the calibrated one. This is acceptable for the auto-approve
    gate: calibrated <= the clamped raw always holds (deflation only ever tightens the
    gate), only the top bucket's calibrated value can ever reach the 0.85
    floor at all, and within a single bucket (a fixed table) the map is
    monotone non-decreasing — a decision made by comparing calibrated values
    against a fixed floor for proposals in the SAME bucket stays ordered
    consistently; it is only a comparison across buckets that can invert.
    """
    if raw is None:
        return None
    try:
        raw_f = float(raw)
    except (TypeError, ValueError):
        return None
    if raw_f != raw_f:  # NaN != NaN
        return None
    raw_f = max(0.0, min(1.0, raw_f))
    b = bucket_for(raw_f)
    if b is None or not bucket_table:
        return raw_f
    _lo, _hi, label = b
    row = next((r for r in bucket_table if r.get('range') == label), None)
    if row is None:
        return raw_f
    try:
        n = int(row.get('count') or 0)
    except (TypeError, ValueError):
        return raw_f
    rate = row.get('match_rate')
    if n < min_n or rate is None:
        return raw_f
    mid = bucket_midpoint(_lo, _hi)
    if mid <= 0:
        return raw_f
    ratio = float(rate) / mid
    ratio = max(0.5, min(1.0, ratio))
    return raw_f * ratio


def evidence_level(n_closed: int, staleness_days,
                   *, stale_days: int = EVIDENCE_STALE_DAYS) -> str:
    """Evidence tier for a proposal's decisive window.

    Count tiers: <10 none, <30 low, <100 medium, else high. A sleeve whose most
    recent closed trade is older than `stale_days` drops exactly one tier
    (floored at 'none'); a sleeve with no closed trade at all is 'none'
    regardless of count (staleness_days is None only in that case).
    """
    if staleness_days is None:
        return 'none'
    try:
        n = int(n_closed or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 10:
        level = 'none'
    elif n < 30:
        level = 'low'
    elif n < 100:
        level = 'medium'
    else:
        level = 'high'
    if float(staleness_days) > float(stale_days):
        idx = max(0, EVIDENCE_LEVELS.index(level) - 1)
        level = EVIDENCE_LEVELS[idx]
    return level


def evidence_cap(n_closed: int, staleness_days) -> tuple:
    """(level, cap) for a proposal's decisive window — see EVIDENCE_CAPS."""
    level = evidence_level(n_closed, staleness_days)
    return level, EVIDENCE_CAPS[level]


def evidence_counts(strategy_id: str, regime_state: str, *,
                    window_days: int = EVIDENCE_WINDOW_DAYS, now=None) -> dict:
    """Closed-trade evidence behind a PENDING proposal for (strategy, regime).

    _direction_match's "decisive window" is defined relative to `decided_at`,
    which a pending proposal does not have yet. The honest analogue at
    auto-approval time is the TRAILING `window_days` of closed trades — the same
    evidence the memo that produced the proposal was written from. Staleness is
    measured against the most recent closed trade with NO lookback bound, so a
    sleeve dark for two months reads as stale even though its trailing-window
    count is 0.

    `signal_pnl.closed_at` is a DATE column (migration
    `012_execution_engine.sql:62`, never altered since), so psycopg2 hands
    back a plain `datetime.date`, not a `datetime.datetime` — a bare
    `.replace(tzinfo=...)` on a `date` raises `TypeError`. Both `closed_at`
    (`last`) and `now` are normalised the same way before any arithmetic: a
    `date` becomes midnight UTC on that date; a naive `datetime` is assumed
    already UTC (the pipeline writes and reads UTC throughout); an aware
    `datetime` is kept as-is. `now=None` defaults to the current UTC instant.

    FAILS CLOSED: any exception raised while talking to the database (bad
    connection, missing table, timeout, ...) is caught, logged at WARNING,
    and this returns `{'n_closed': 0, 'staleness_days': None}` — the 'none'
    evidence level, cap 0.35 — rather than raising or fabricating a
    permissive count. Task 5's auto-approve path depends on this: an unknown
    evidence state must never read as strong evidence, and a DB blip must
    never crash the proposal pipeline.

    Returns {'n_closed': int, 'staleness_days': float | None}; staleness is
    None when the sleeve has no closed trade at all (or on DB failure).
    """
    import logging
    from datetime import date, datetime, time as _time, timedelta, timezone

    def _aware_utc(value):
        """Normalise a DB DATE/TIMESTAMP value (or `now`) to an aware UTC
        datetime; None stays None."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if isinstance(value, date):
            return datetime.combine(value, _time.min, tzinfo=timezone.utc)
        return value

    ref = _aware_utc(now) or datetime.now(timezone.utc)
    sql = """
        SELECT COUNT(*) FILTER (WHERE sp.closed_at >= %s) AS n_closed,
               MAX(sp.closed_at)                          AS last_closed_at
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE es.strategy_id = %s
           AND es.regime_state = %s
           AND sp.realized_pnl_pct IS NOT NULL
    """
    window_start = ref - timedelta(days=int(window_days))
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (window_start, strategy_id, regime_state))
                row = cur.fetchone()
    except Exception:
        logging.getLogger(__name__).warning(
            "evidence_counts: DB error for strategy_id=%s regime_state=%s — "
            "failing closed (n_closed=0, staleness_days=None, evidence level "
            "'none', cap 0.35)", strategy_id, regime_state, exc_info=True)
        return {'n_closed': 0, 'staleness_days': None}

    n_closed = int(row[0] or 0) if row else 0
    last = _aware_utc(row[1] if row else None)
    staleness = (ref - last).total_seconds() / 86400.0 if last is not None else None
    return {'n_closed': n_closed, 'staleness_days': staleness}


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
    resolved = [o for o in obs if o.get('direction_match') is not None]
    hit_rate = (sum(1 for o in resolved if o['direction_match']) / len(resolved)) if resolved else None
    mean_conf = (sum(float(o['confidence']) for o in resolved) / len(resolved)) if resolved else None
    return {
        'total_observations':    len(obs),
        'resolved_observations': len(resolved),
        'hit_rate':              hit_rate,
        'mean_confidence':       mean_conf,
        'brier_score':           brier,
        'buckets':               _bucket_aggregates(obs),
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
