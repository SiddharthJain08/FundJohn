#!/usr/bin/env python3
"""Bootstrap Monte Carlo for size_scalar proposals.

Given a (strategy, regime) realized PnL distribution, estimates the
confidence interval on (Sharpe, mean PnL, max DD) under a proposed size
scalar via the linear-scaling assumption.

Caveats (read before trusting):
1. **Linear scaling**: doubling size doubles each trade's PnL. True for
   delta-1 strategies with proportional stops; false for fixed-stop
   strategies (which get disproportionately hurt by larger sizing).
2. **No path simulation**: max-DD is computed per-bootstrap on cumulative
   sum of resampled trades, NOT on real intraday paths. Realistic max-DD
   needs intraday bars + per-bar stop checking. Phase 2E if pursued.
3. **Bootstrap CI** captures variance from the *observed* sample. If the
   strategy's true distribution differs from the sample (regime shift,
   etc.), CI is misleading.

Spec: docs/archive/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2d-design.md
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MIN_TRADES_FOR_MC = 10
DEFAULT_N_ITER = 1000
PERCENTILES = (5, 50, 95)
TRADING_DAYS_PER_YEAR = 252


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _percentile(sorted_values: list[float], p: int) -> float:
    if not sorted_values:
        return float('nan')
    k = (len(sorted_values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def _bootstrap_sharpe(pnls: list[float], rng: random.Random) -> float:
    sample = [rng.choice(pnls) for _ in range(len(pnls))]
    mean = sum(sample) / len(sample)
    if len(sample) < 2:
        return 0.0
    var = sum((x - mean) ** 2 for x in sample) / (len(sample) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(TRADING_DAYS_PER_YEAR / max(len(sample), 1.0))


def _bootstrap_max_dd(pnls: list[float], rng: random.Random) -> float:
    """Worst peak-to-trough on the bootstrap cumulative path. Returns
    negative value (drawdown magnitude in PnL %)."""
    sample = [rng.choice(pnls) for _ in range(len(pnls))]
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in sample:
        cum += x
        if cum > peak:
            peak = cum
        dd = cum - peak
        if dd < max_dd:
            max_dd = dd
    return max_dd


def bootstrap_pnls(pnls: list[float], ratio: float = 1.0,
                    n_iter: int = DEFAULT_N_ITER,
                    seed: Optional[int] = None) -> dict:
    """Run bootstrap on a list of realized PnL pcts.

    `ratio`: applied linearly to every PnL before aggregating (size scalar
    proposed / current).
    """
    if len(pnls) < MIN_TRADES_FOR_MC:
        return {
            'status': 'INSUFFICIENT',
            'n_trades_sampled': len(pnls),
            'note': f'need >= {MIN_TRADES_FOR_MC} trades for bootstrap MC',
        }
    if n_iter <= 0:
        return {'status': 'INSUFFICIENT', 'n_iter': 0,
                'note': 'n_iter must be positive'}

    scaled = [p * ratio for p in pnls]
    rng = random.Random(seed) if seed is not None else random.Random()

    sharpes: list[float] = []
    means: list[float] = []
    max_dds: list[float] = []
    for _ in range(n_iter):
        sharpes.append(_bootstrap_sharpe(scaled, rng))
        rng2 = random.Random(rng.random())
        means.append(sum(rng2.choice(scaled) for _ in range(len(scaled))) / len(scaled))
        max_dds.append(_bootstrap_max_dd(scaled, rng))

    sharpes.sort(); means.sort(); max_dds.sort()
    out = {
        'status':           'OK',
        'n_trades_sampled': len(pnls),
        'n_iter':           n_iter,
        'ratio_applied':    ratio,
    }
    for p in PERCENTILES:
        out[f'sharpe_p{p:02d}']   = _percentile(sharpes, p)
        out[f'mean_pnl_p{p:02d}'] = _percentile(means, p)
        out[f'max_dd_p{p:02d}']   = _percentile(max_dds, p)
    return out


def _load_realized_pnls(strategy_id: str, regime_state: str,
                          window_days: int = 365) -> list[float]:
    sql = """
        SELECT sp.realized_pnl_pct::float
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE es.strategy_id = %s
           AND es.regime_state = %s
           AND sp.realized_pnl_pct IS NOT NULL
           AND sp.closed_at IS NOT NULL
           AND sp.closed_at >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (strategy_id, regime_state, window_days))
            return [float(r[0]) for r in cur.fetchall()]


def bootstrap_size_scalar(strategy_id: str, regime_state: str,
                           current_size: float, proposed_size: float,
                           n_iter: int = DEFAULT_N_ITER,
                           window_days: int = 365,
                           seed: Optional[int] = None,
                           proposal_id: Optional[int] = None,
                           persist: bool = True) -> dict:
    """End-to-end: load realized PnLs, bootstrap under the size ratio,
    optionally persist to strategy_regime_mc_runs."""
    pnls = _load_realized_pnls(strategy_id, regime_state, window_days)
    ratio = float(proposed_size) / max(float(current_size), 0.001)
    result = bootstrap_pnls(pnls, ratio=ratio, n_iter=n_iter, seed=seed)
    result.update({
        'strategy_id':   strategy_id,
        'regime_state':  regime_state,
        'current_size':  current_size,
        'proposed_size': proposed_size,
        'window_days':   window_days,
    })

    if persist and result.get('status') == 'OK':
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO strategy_regime_mc_runs
                        (strategy_id, regime_state, current_size, proposed_size,
                         n_trades_sampled, n_bootstrap_iter,
                         sharpe_p05, sharpe_p50, sharpe_p95,
                         mean_pnl_p05, mean_pnl_p50, mean_pnl_p95,
                         max_dd_p05, max_dd_p50, max_dd_p95,
                         proposal_id)
                    VALUES (%s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (strategy_id, regime_state, current_size, proposed_size,
                      result['n_trades_sampled'], n_iter,
                      result['sharpe_p05'], result['sharpe_p50'], result['sharpe_p95'],
                      result['mean_pnl_p05'], result['mean_pnl_p50'], result['mean_pnl_p95'],
                      result['max_dd_p05'], result['max_dd_p50'], result['max_dd_p95'],
                      proposal_id))
            conn.commit()
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--strategy', required=True)
    p.add_argument('--regime', required=True)
    p.add_argument('--current', type=float, required=True)
    p.add_argument('--proposed', type=float, required=True)
    p.add_argument('--n-iter', type=int, default=DEFAULT_N_ITER)
    p.add_argument('--window-days', type=int, default=365)
    p.add_argument('--seed', type=int, default=None)
    p.add_argument('--no-persist', action='store_true')
    args = p.parse_args()
    result = bootstrap_size_scalar(
        strategy_id=args.strategy, regime_state=args.regime,
        current_size=args.current, proposed_size=args.proposed,
        n_iter=args.n_iter, window_days=args.window_days,
        seed=args.seed, persist=not args.no_persist,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
