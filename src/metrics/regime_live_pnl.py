#!/usr/bin/env python3
"""Nightly rollup of per-strategy×regime live PnL from signal_pnl.

Joins signal_pnl (realized rows only) with execution_signals to attach the
regime_state observed at signal time, then aggregates by
(strategy_id, regime_state, window_days). One row per group per nightly run.

Window 0 = all time. Other windows are inclusive of the last N days by
closed_at. This is the data source for:
  - Dashboard "Regime Eligibility" tab (Phase 1)
  - Future learned-sizer training input (Phase 2)

Run as CLI:
    python -m metrics.regime_live_pnl --windows 30 90 0
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
DEFAULT_WINDOWS = (30, 90, 0)
TRADING_DAYS_PER_YEAR = 252


def _db_uri() -> str:
    return (
        os.environ.get('DATABASE_URL')
        or os.environ.get('POSTGRES_URI')
        or 'postgresql://openclaw:password@localhost:5432/openclaw'
    )


def _connect(uri: str):
    import psycopg2
    return psycopg2.connect(uri)


def load_closed_trades(uri: str) -> pd.DataFrame:
    """All closed signal_pnl rows joined with execution_signals.regime_state."""
    sql = """
        SELECT
            es.strategy_id,
            es.regime_state,
            es.signal_date,
            sp.closed_at,
            sp.realized_pnl_pct::float AS realized_pnl_pct,
            COALESCE(sp.days_held, 0) AS days_held
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE sp.realized_pnl_pct IS NOT NULL
           AND sp.closed_at IS NOT NULL
           AND es.regime_state IS NOT NULL
    """
    with _connect(uri) as conn:
        return pd.read_sql(sql, conn)


def compute_rollup(df: pd.DataFrame, windows=DEFAULT_WINDOWS,
                    today: date | None = None) -> pd.DataFrame:
    """Aggregate trades by (strategy_id, regime_state, window_days).

    A window of 0 means "all time".
    """
    if df.empty:
        return pd.DataFrame()
    today = today or date.today()
    df = df.copy()
    df['closed_at'] = pd.to_datetime(df['closed_at']).dt.date

    rows: list[dict] = []
    for window in windows:
        if window == 0:
            sub = df
        else:
            cutoff = today - timedelta(days=window)
            sub = df[df['closed_at'] >= cutoff]
        if sub.empty:
            continue
        grouped = sub.groupby(['strategy_id', 'regime_state'], dropna=False)
        for (strategy_id, regime_state), g in grouped:
            pnls = g['realized_pnl_pct'].astype(float)
            avg = float(pnls.mean())
            std = float(pnls.std(ddof=0)) if len(pnls) > 1 else 0.0
            avg_hold = float(g['days_held'].mean()) if len(g) else 0.0
            sharpe_proxy: float | None
            if std > 0 and avg_hold > 0:
                periods_per_year = TRADING_DAYS_PER_YEAR / max(avg_hold, 1.0)
                sharpe_proxy = (avg / std) * math.sqrt(periods_per_year)
            else:
                sharpe_proxy = None
            last_signal = pd.to_datetime(g['signal_date']).max()
            if last_signal is not None and getattr(last_signal, 'tzinfo', None) is None:
                last_signal = last_signal.tz_localize('UTC')
            rows.append({
                'strategy_id':    strategy_id,
                'regime_state':   regime_state,
                'window_days':    window,
                'trade_count':    int(len(g)),
                'win_count':      int((pnls > 0).sum()),
                'total_pnl_pct':  float(pnls.sum()),
                'avg_pnl_pct':    avg,
                'stdev_pnl_pct':  std,
                'sharpe_proxy':   sharpe_proxy,
                'max_dd_proxy':   float(pnls.min()),
                'avg_hold_days':  avg_hold,
                'last_signal_at': last_signal,
            })
    return pd.DataFrame(rows)


def persist_rollup(df: pd.DataFrame, uri: str,
                    run_at: datetime | None = None) -> int:
    """Insert rollup rows. Returns number of rows inserted."""
    if df.empty:
        return 0
    run_at = run_at or datetime.now(timezone.utc)
    sql = """
        INSERT INTO strategy_regime_live_pnl_rollup (
            run_at, strategy_id, regime_state, window_days,
            trade_count, win_count, total_pnl_pct, avg_pnl_pct,
            stdev_pnl_pct, sharpe_proxy, max_dd_proxy, avg_hold_days,
            last_signal_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    rows = []
    for _, r in df.iterrows():
        sp = r['sharpe_proxy']
        if sp is None or (isinstance(sp, float) and math.isnan(sp)):
            sp_val = None
        else:
            sp_val = float(sp)
        rows.append((
            run_at,
            r['strategy_id'], r['regime_state'], int(r['window_days']),
            int(r['trade_count']), int(r['win_count']),
            float(r['total_pnl_pct']), float(r['avg_pnl_pct']),
            float(r['stdev_pnl_pct']), sp_val,
            float(r['max_dd_proxy']), float(r['avg_hold_days']),
            r['last_signal_at'],
        ))
    with _connect(uri) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
    return len(rows)


def run(uri: str | None = None,
        windows=DEFAULT_WINDOWS,
        today: date | None = None) -> dict:
    uri = uri or _db_uri()
    trades = load_closed_trades(uri)
    rollup = compute_rollup(trades, windows=windows, today=today)
    inserted = persist_rollup(rollup, uri=uri)
    return {
        'closed_trades_loaded': int(len(trades)),
        'rollup_rows':          int(len(rollup)),
        'inserted':             inserted,
        'windows':              list(windows),
    }


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    p.add_argument('--windows', nargs='+', type=int, default=list(DEFAULT_WINDOWS))
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    uri = _db_uri()
    trades = load_closed_trades(uri)
    rollup = compute_rollup(trades, windows=args.windows)
    print(rollup.to_string(index=False))
    if args.dry_run:
        print(f'\n[dry-run] would insert {len(rollup)} rows')
        return 0
    inserted = persist_rollup(rollup, uri=uri)
    print(f'\n[rollup] inserted {inserted} rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
