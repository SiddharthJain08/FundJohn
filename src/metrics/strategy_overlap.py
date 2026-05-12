#!/usr/bin/env python3
"""Pairwise strategy signal overlap.

For every pair of strategies that fired on the same (date, ticker, regime),
count the overlap and compute Jaccard index. Populates
`strategy_signal_overlap` nightly. Surface for future correlation-adjusted
sizing (out of scope in 2D).

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2d-design.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Optional

DEFAULT_WINDOW_DAYS = 90


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _load_signals(window_days: int) -> list[dict]:
    sql = """
        SELECT strategy_id, signal_date, ticker, regime_state
          FROM execution_signals
         WHERE signal_date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
           AND strategy_id IS NOT NULL
           AND ticker IS NOT NULL
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (window_days,))
            cols = ('strategy_id', 'signal_date', 'ticker', 'regime_state')
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def aggregate_overlap(signals: list[dict]) -> list[dict]:
    """Compute pairwise overlap from a list of signal records.

    Emits two rows per pair: one regime-specific, one regime=NULL aggregate.
    Always emits strategy_a < strategy_b.
    """
    if not signals:
        return []
    # Per-strategy counts
    by_strategy: dict[str, int] = defaultdict(int)
    by_strategy_regime: dict[tuple[str, str], int] = defaultdict(int)
    # (date, ticker, regime) -> list of strategy_ids
    by_event: dict[tuple, set] = defaultdict(set)
    by_event_any_regime: dict[tuple, set] = defaultdict(set)
    for sig in signals:
        sid = sig['strategy_id']
        regime = sig.get('regime_state')
        by_strategy[sid] += 1
        by_strategy_regime[(sid, regime)] += 1
        key = (sig.get('signal_date'), sig.get('ticker'), regime)
        by_event[key].add(sid)
        key_any = (sig.get('signal_date'), sig.get('ticker'))
        by_event_any_regime[key_any].add(sid)

    overlap_by_pair: dict[tuple, int] = defaultdict(int)
    overlap_by_pair_any: dict[tuple, int] = defaultdict(int)
    for (date_, ticker, regime), strategies in by_event.items():
        ss = sorted(strategies)
        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                overlap_by_pair[(ss[i], ss[j], regime)] += 1
    for (date_, ticker), strategies in by_event_any_regime.items():
        ss = sorted(strategies)
        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                overlap_by_pair_any[(ss[i], ss[j])] += 1

    out: list[dict] = []
    for (a, b, regime), overlap in overlap_by_pair.items():
        a_count = by_strategy_regime.get((a, regime), 0)
        b_count = by_strategy_regime.get((b, regime), 0)
        union = a_count + b_count - overlap
        out.append({
            'strategy_a': a, 'strategy_b': b, 'regime_state': regime,
            'overlap_count': overlap, 'a_signal_count': a_count,
            'b_signal_count': b_count,
            'jaccard_idx': overlap / union if union > 0 else 0.0,
        })
    for (a, b), overlap in overlap_by_pair_any.items():
        a_count = by_strategy.get(a, 0)
        b_count = by_strategy.get(b, 0)
        union = a_count + b_count - overlap
        out.append({
            'strategy_a': a, 'strategy_b': b, 'regime_state': None,
            'overlap_count': overlap, 'a_signal_count': a_count,
            'b_signal_count': b_count,
            'jaccard_idx': overlap / union if union > 0 else 0.0,
        })
    return out


def compute_overlap(window_days: int = DEFAULT_WINDOW_DAYS) -> int:
    """End-to-end: load signals, aggregate, persist. Returns rows inserted."""
    signals = _load_signals(window_days)
    rows = aggregate_overlap(signals)
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    sql = """
        INSERT INTO strategy_signal_overlap
            (computed_at, window_days, strategy_a, strategy_b, regime_state,
             overlap_count, a_signal_count, b_signal_count, jaccard_idx)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (computed_at, window_days, strategy_a, strategy_b, regime_state)
        DO NOTHING
    """
    n = 0
    with _connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(sql, (now, window_days, r['strategy_a'], r['strategy_b'],
                                   r['regime_state'], r['overlap_count'],
                                   r['a_signal_count'], r['b_signal_count'],
                                   r['jaccard_idx']))
                n += cur.rowcount
        conn.commit()
    return n


def latest_overlaps(top_n: int = 20, min_overlap: int = 2) -> list[dict]:
    """Top-N strategy pairs by Jaccard, from the most recent run, regime-collapsed."""
    sql = """
        SELECT strategy_a, strategy_b, regime_state,
               overlap_count, a_signal_count, b_signal_count, jaccard_idx::float
          FROM strategy_signal_overlap
         WHERE computed_at = (SELECT MAX(computed_at) FROM strategy_signal_overlap)
           AND regime_state IS NULL
           AND overlap_count >= %s
         ORDER BY jaccard_idx DESC NULLS LAST
         LIMIT %s
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (min_overlap, top_n))
            cols = ('strategy_a', 'strategy_b', 'regime_state',
                    'overlap_count', 'a_signal_count', 'b_signal_count',
                    'jaccard_idx')
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    p = argparse.ArgumentParser()
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument('--compute', action='store_true')
    sub.add_argument('--top', type=int, default=None)
    p.add_argument('--window', type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument('--min-overlap', type=int, default=2)
    args = p.parse_args()
    if args.compute:
        n = compute_overlap(window_days=args.window)
        print(f'inserted {n} overlap rows')
        return 0
    if args.top:
        rows = latest_overlaps(top_n=args.top, min_overlap=args.min_overlap)
        for r in rows:
            print(f"{r['strategy_a']} ↔ {r['strategy_b']:<40} "
                  f"jaccard={r['jaccard_idx']:.3f} "
                  f"overlap={r['overlap_count']} "
                  f"({r['a_signal_count']}/{r['b_signal_count']})")
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
