#!/usr/bin/env python3
"""Live-vs-baseline drift detector for per-(strategy, regime) performance.

Compares each row in strategy_regime_live_pnl_rollup (latest snapshot)
against:
  1. strategy_regime_priors (literature/research prior — if set)
  2. strategy_regime_param_proposals.applied_row (most recent approved
     change for this pair — if set)

Emits one drift signal per (strategy, regime) where at least one baseline
is present and live trade_count >= MIN_TRADES.

Spec: docs/archive/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2c-design.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

# Severity thresholds
SHARPE_WARN_DELTA = 0.5      # |live - baseline|
SHARPE_FAIL_DELTA = 1.0
WIN_RATE_WARN_DELTA = 0.10
WIN_RATE_FAIL_DELTA = 0.20
MIN_TRADES = 10              # below this, severity = 'INSUFFICIENT'


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _load_live_rollup() -> list[dict]:
    """Latest snapshot from strategy_regime_live_pnl_rollup; 90d window preferred."""
    sql = """
        SELECT strategy_id, regime_state,
               sharpe_proxy::float    AS sharpe_proxy,
               (CASE WHEN trade_count > 0
                     THEN win_count::float / trade_count
                     ELSE 0 END)::float AS win_rate,
               avg_pnl_pct::float     AS avg_pnl_pct,
               trade_count
          FROM strategy_regime_live_pnl_rollup
         WHERE window_days = 90
           AND run_at = (SELECT MAX(run_at) FROM strategy_regime_live_pnl_rollup)
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = ('strategy_id', 'regime_state', 'sharpe_proxy',
                    'win_rate', 'avg_pnl_pct', 'trade_count')
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def _load_priors() -> dict:
    """Map (strategy_id, regime_state) → prior dict."""
    sql = """
        SELECT strategy_id, regime_state,
               expected_sharpe::float    AS expected_sharpe,
               expected_win_rate::float  AS expected_win_rate,
               expected_avg_pnl_pct::float AS expected_avg_pnl_pct,
               source
          FROM strategy_regime_priors
    """
    out: dict = {}
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for sid, regime, sh, wr, avg, src in cur.fetchall():
                out[(sid, regime)] = {
                    'expected_sharpe':      sh,
                    'expected_win_rate':    wr,
                    'expected_avg_pnl_pct': avg,
                    'source':               src,
                }
    return out


def _load_applied_baselines() -> dict:
    """Map (strategy_id, regime_state) → most-recent approved applied_row."""
    sql = """
        SELECT DISTINCT ON (strategy_id, regime_state)
               strategy_id, regime_state, applied_row
          FROM strategy_regime_param_proposals
         WHERE status IN ('approved', 'modified')
           AND applied_row IS NOT NULL
         ORDER BY strategy_id, regime_state, decided_at DESC
    """
    out: dict = {}
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for sid, regime, applied in cur.fetchall():
                out[(sid, regime)] = applied or {}
    return out


def _severity_for_deltas(sharpe_delta: Optional[float],
                          win_rate_delta: Optional[float]) -> str:
    abs_s = abs(sharpe_delta) if sharpe_delta is not None else 0.0
    abs_w = abs(win_rate_delta) if win_rate_delta is not None else 0.0
    if abs_s >= SHARPE_FAIL_DELTA or abs_w >= WIN_RATE_FAIL_DELTA:
        return 'FAIL'
    if abs_s >= SHARPE_WARN_DELTA or abs_w >= WIN_RATE_WARN_DELTA:
        return 'WARN'
    return 'OK'


def compute_drift(strategy_id: Optional[str] = None,
                  regime_state: Optional[str] = None) -> list[dict]:
    """Return drift signals for filtered (strategy, regime) pairs."""
    live_rows = _load_live_rollup()
    priors = _load_priors()
    applied = _load_applied_baselines()
    out: list[dict] = []
    for row in live_rows:
        sid, regime = row['strategy_id'], row['regime_state']
        if strategy_id and sid != strategy_id:
            continue
        if regime_state and regime != regime_state:
            continue
        baseline = priors.get((sid, regime))
        baseline_source = baseline['source'] if baseline else None
        if baseline is None:
            # No literature prior — fall back to most recent applied row?
            # applied_row stores only the param values (eligible/size/...)
            # not expected performance, so it doesn't give us an
            # expected-Sharpe baseline. Skip if no prior.
            if (sid, regime) not in applied:
                continue
            continue  # applied baseline ≠ performance baseline; skip without prior

        trade_count = row['trade_count'] or 0
        if trade_count < MIN_TRADES:
            severity = 'INSUFFICIENT'
            sharpe_delta = None
            wr_delta = None
            reason = f'only {trade_count} closed trades (min {MIN_TRADES})'
        else:
            live_sharpe = row['sharpe_proxy']
            live_wr     = row['win_rate']
            exp_sharpe  = baseline['expected_sharpe']
            exp_wr      = baseline['expected_win_rate']
            sharpe_delta = (live_sharpe - exp_sharpe) if (live_sharpe is not None and exp_sharpe is not None) else None
            wr_delta     = (live_wr     - exp_wr)     if (live_wr     is not None and exp_wr     is not None) else None
            severity = _severity_for_deltas(sharpe_delta, wr_delta)
            reason = (
                f'live sharpe={live_sharpe:.2f} vs prior {exp_sharpe} '
                f'(Δ={sharpe_delta:+.2f}); '
                f'win {(live_wr or 0)*100:.0f}% vs {(exp_wr or 0)*100:.0f}% '
                f'over {trade_count} trades; source={baseline_source}'
            )
        out.append({
            'strategy_id':       sid,
            'regime_state':      regime,
            'live_sharpe':       row['sharpe_proxy'],
            'live_win_rate':     row['win_rate'],
            'live_avg_pnl_pct':  row['avg_pnl_pct'],
            'live_trade_count':  trade_count,
            'prior_sharpe':      baseline.get('expected_sharpe'),
            'prior_win_rate':    baseline.get('expected_win_rate'),
            'prior_source':      baseline_source,
            'sharpe_delta':      sharpe_delta,
            'win_rate_delta':    wr_delta,
            'severity':          severity,
            'reason':            reason,
        })
    return out


def latest_drift_summary() -> dict:
    sigs = compute_drift()
    out = {'OK': 0, 'WARN': 0, 'FAIL': 0, 'INSUFFICIENT': 0}
    for s in sigs:
        out[s['severity']] = out.get(s['severity'], 0) + 1
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--strategy', default=None)
    p.add_argument('--regime', default=None)
    p.add_argument('--summary', action='store_true')
    args = p.parse_args()
    if args.summary:
        print(json.dumps(latest_drift_summary(), indent=2))
        return 0
    sigs = compute_drift(strategy_id=args.strategy, regime_state=args.regime)
    for s in sigs:
        print(f"[{s['severity']:<12}] {s['strategy_id']:<40} {s['regime_state']:<14} {s['reason']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
