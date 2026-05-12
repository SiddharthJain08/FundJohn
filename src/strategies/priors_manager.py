#!/usr/bin/env python3
"""Per-(strategy, regime) literature/research priors.

Sparse: populated only for pairs where research or operator has set an
expected baseline (Sharpe, win-rate, avg pnl). Consumed by the drift
detector to compare live performance against the prior.

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2c-design.md
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


def _to_jsonb(d):
    if d is None:
        return None
    cleaned = {}
    for k, v in d.items():
        if hasattr(v, '__float__') and not isinstance(v, (int, bool)):
            try:
                cleaned[k] = float(v)
            except (TypeError, ValueError):
                cleaned[k] = str(v)
        else:
            cleaned[k] = v
    return json.dumps(cleaned, default=str)


_COLS = ('strategy_id', 'regime_state', 'expected_sharpe', 'expected_win_rate',
         'expected_avg_pnl_pct', 'source', 'confidence', 'notes', 'set_by')


def set_prior(*,
              strategy_id: str,
              regime_state: str,
              expected_sharpe: Optional[float],
              expected_win_rate: Optional[float],
              expected_avg_pnl_pct: Optional[float],
              source: str,
              confidence: Optional[float],
              notes: Optional[str],
              actor: str,
              reason: str = '') -> dict:
    """Upsert a (strategy, regime) prior. Audits before write."""
    if regime_state not in CANONICAL_REGIMES:
        raise ValueError(f'invalid regime {regime_state!r}')
    if not source or not str(source).strip():
        raise ValueError("source is required (e.g. paper citation, 'operator:internal')")

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, regime_state, expected_sharpe,
                       expected_win_rate, expected_avg_pnl_pct,
                       source, confidence, notes, set_by
                  FROM strategy_regime_priors
                 WHERE strategy_id = %s AND regime_state = %s
                 FOR UPDATE
            """, (strategy_id, regime_state))
            before = cur.fetchone()
            before_dict = (None if before is None
                            else dict(zip(_COLS, before)))
            after_dict = dict(zip(_COLS,
                (strategy_id, regime_state, expected_sharpe,
                 expected_win_rate, expected_avg_pnl_pct,
                 source, confidence, notes, actor)))

            cur.execute("""
                INSERT INTO strategy_regime_priors_changes
                    (actor, strategy_id, regime_state, before_row, after_row, reason)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)
            """, (actor, strategy_id, regime_state,
                  _to_jsonb(before_dict), _to_jsonb(after_dict), reason))

            cur.execute("""
                INSERT INTO strategy_regime_priors
                    (strategy_id, regime_state,
                     expected_sharpe, expected_win_rate, expected_avg_pnl_pct,
                     source, confidence, notes, set_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (strategy_id, regime_state) DO UPDATE
                   SET expected_sharpe      = EXCLUDED.expected_sharpe,
                       expected_win_rate    = EXCLUDED.expected_win_rate,
                       expected_avg_pnl_pct = EXCLUDED.expected_avg_pnl_pct,
                       source               = EXCLUDED.source,
                       confidence           = EXCLUDED.confidence,
                       notes                = EXCLUDED.notes,
                       set_at               = NOW(),
                       set_by               = EXCLUDED.set_by
            """, (strategy_id, regime_state, expected_sharpe, expected_win_rate,
                  expected_avg_pnl_pct, source, confidence, notes, actor))
        conn.commit()

    return {
        'strategy_id': strategy_id, 'regime_state': regime_state,
        'before': before_dict, 'after': after_dict,
        'audited_at': datetime.now(timezone.utc).isoformat(),
    }


def get_prior(strategy_id: str, regime_state: str) -> Optional[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, regime_state, expected_sharpe,
                       expected_win_rate, expected_avg_pnl_pct,
                       source, confidence, notes, set_at, set_by
                  FROM strategy_regime_priors
                 WHERE strategy_id = %s AND regime_state = %s
            """, (strategy_id, regime_state))
            row = cur.fetchone()
    if row is None:
        return None
    cols = ('strategy_id', 'regime_state', 'expected_sharpe',
            'expected_win_rate', 'expected_avg_pnl_pct', 'source',
            'confidence', 'notes', 'set_at', 'set_by')
    return dict(zip(cols, row))


def list_priors() -> list[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, regime_state,
                       expected_sharpe, expected_win_rate, expected_avg_pnl_pct,
                       source, confidence, notes, set_at, set_by
                  FROM strategy_regime_priors
                 ORDER BY strategy_id, regime_state
            """)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument('--list', action='store_true')
    sub.add_argument('--set', nargs=2, metavar=('STRATEGY', 'REGIME'))
    p.add_argument('--sharpe', type=float, default=None)
    p.add_argument('--win-rate', type=float, default=None)
    p.add_argument('--avg-pnl', type=float, default=None)
    p.add_argument('--source', default=None)
    p.add_argument('--confidence', type=float, default=None)
    p.add_argument('--notes', default=None)
    p.add_argument('--actor', default='cli')
    p.add_argument('--reason', default='')
    args = p.parse_args()
    if args.list:
        for r in list_priors():
            print(f"{r['strategy_id']:<40} {r['regime_state']:<14} "
                  f"sh={r['expected_sharpe']} wr={r['expected_win_rate']} "
                  f"avg={r['expected_avg_pnl_pct']} src={r['source']}")
        return 0
    if args.set:
        strategy, regime = args.set
        if not args.source:
            print('--source required (e.g. paper citation)', file=sys.stderr)
            return 2
        result = set_prior(strategy_id=strategy, regime_state=regime,
                           expected_sharpe=args.sharpe,
                           expected_win_rate=args.win_rate,
                           expected_avg_pnl_pct=args.avg_pnl,
                           source=args.source,
                           confidence=args.confidence,
                           notes=args.notes,
                           actor=args.actor, reason=args.reason)
        print(json.dumps(result, indent=2, default=str))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
