"""
bt_str06.py — Backtest harness for S-TR-06 Baltussen EOD Reversal.

Synthetic mode: generate 30-min bars for ~30 names per day with a small
mean-reversion component built into the 15:30 → 16:00 bar.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from datetime import time

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_framework import (
    Backtester, BacktestConfig, IntradayTrade,
)

VPS_DATA_DIR = Path("/root/openclaw/data/master")
PRICES_30M_PARQ = VPS_DATA_DIR / "prices_30m.parquet"


def load_real_30m() -> pd.DataFrame | None:
    if not PRICES_30M_PARQ.exists():
        return None
    df = pd.read_parquet(PRICES_30M_PARQ)
    df['datetime'] = pd.to_datetime(df['datetime'])
    return df.sort_values(['ticker', 'datetime']).reset_index(drop=True)


def gen_synthetic_30m(n_days: int = 750, n_tickers: int = 30,
                      seed: int = 21) -> pd.DataFrame:
    """13 30-min bars per session for each of n_tickers names with mild
    EOD mean-reversion built in (last bar reverses 25% of intraday drift)."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2022-01-01', periods=n_days, freq='B')
    tickers = [f"T{idx:03d}" for idx in range(n_tickers)]
    bars_per_day = 13
    rows = []
    for tk in tickers:
        last_close = float(rng.uniform(50, 400))
        for d in dates:
            gap = rng.normal(0.0, 0.005)
            today_open = last_close * (1 + gap)
            eps = rng.normal(0.0, 0.0025, bars_per_day - 1)
            # Build prices through 15:30
            prices = [today_open]
            for k in range(bars_per_day - 1):
                prices.append(prices[-1] * (1 + eps[k]))
            r_total = prices[-1] / today_open - 1
            # 15:30 → 16:00 bar reverses 25 % of r_total (the EOD reversion)
            last_bar_ret = -0.25 * r_total + rng.normal(0.0, 0.0015)
            prices.append(prices[-1] * (1 + last_bar_ret))
            for k in range(bars_per_day):
                ts = pd.Timestamp.combine(d.date(), time(9, 30)) \
                     + pd.Timedelta(minutes=30 * k)
                o = today_open if k == 0 else prices[k]
                c = prices[k + 1] if k + 1 < len(prices) else prices[k]
                h = max(o, c) * 1.0005
                l = min(o, c) * 0.9995
                v = int(rng.integers(500_000, 4_000_000))
                rows.append({'ticker': tk, 'datetime': ts,
                             'open': o, 'high': h, 'low': l, 'close': c,
                             'volume': v})
            last_close = prices[-1]
    return pd.DataFrame(rows)


def run_backtest(bars: pd.DataFrame,
                 intraday_drift_min: float = 0.010,
                 basket_per_side: int = 5) -> dict:
    bars = bars.copy()
    bars['date'] = bars['datetime'].dt.date

    bt = Backtester(BacktestConfig(annualisation=252, fee_bps=1.0),
                    name='S-TR06_baltussen_eod_reversal')

    n_trades = 0
    for d, dayb in bars.groupby('date'):
        # For each ticker, ensure 13 bars
        dayb_pivot = dayb.pivot(index='ticker', columns='datetime', values='close')
        if dayb_pivot.shape[1] < 12:
            continue
        dayb_pivot = dayb_pivot.sort_index(axis=1)
        cols = list(dayb_pivot.columns)

        # Identify the 09:30 open, 13:00, 15:30, 16:00 bars by index
        try:
            p_open = dayb[dayb['datetime'] == cols[0]].set_index('ticker')['open']
        except Exception:
            continue
        c_close = dayb_pivot.iloc[:, -1]              # 16:00 close
        c_late = dayb_pivot.iloc[:, -2]               # 15:30 close
        # Compute intraday drift through 15:30
        valid = (~p_open.isna()) & (~c_late.isna()) & (~c_close.isna())
        df = pd.DataFrame({
            'p_open': p_open[valid],
            'c_late': c_late[valid],
            'c_close': c_close[valid],
        })
        df['r_total'] = df['c_late'] / df['p_open'] - 1
        df = df[df['r_total'].abs() >= intraday_drift_min]
        if df.empty:
            continue

        # Bottom-N (most down) → LONG; top-N (most up) → SHORT
        df_sorted = df.sort_values('r_total')
        bottoms = df_sorted.head(basket_per_side)
        tops = df_sorted.tail(basket_per_side)

        for tk, row in bottoms.iterrows():
            bt.add_trade(IntradayTrade(
                ticker=tk,
                entry_dt=cols[-2], exit_dt=cols[-1],
                direction='LONG',
                entry_price=float(row['c_late']),
                exit_price=float(row['c_close']),
                weight=0.005,
                label='S-TR06-LONG',
            ))
            n_trades += 1
        for tk, row in tops.iterrows():
            bt.add_trade(IntradayTrade(
                ticker=tk,
                entry_dt=cols[-2], exit_dt=cols[-1],
                direction='SHORT',
                entry_price=float(row['c_late']),
                exit_price=float(row['c_close']),
                weight=0.005,
                label='S-TR06-SHORT',
            ))
            n_trades += 1

    return {'report': bt.report(), 'n_trades_total': n_trades}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--real', action='store_true')
    p.add_argument('--out', type=str, default='results/bt_str06_synthetic.json')
    args = p.parse_args()

    if args.real and PRICES_30M_PARQ.exists():
        bars = load_real_30m(); mode = 'real'
    else:
        bars = gen_synthetic_30m(); mode = 'synthetic'

    print(f"S-TR06 backtest — mode={mode}")
    print(f"  bars : {len(bars):,}")
    out = run_backtest(bars)
    rep = out['report']
    print(f"\n  n_trades : {out['n_trades_total']}")
    print(f"  Sharpe   : {rep.sharpe:.3f}  (IS {rep.is_sharpe:.3f}  OOS {rep.oos_sharpe:.3f})")
    print(f"  Max DD   : {rep.max_dd*100:.1f}%   Win: {rep.win_rate*100:.1f}%")
    print(f"  Cum/Ann  : {rep.cum_return*100:.1f}% / {rep.annualised_return*100:.1f}%")

    out_path = Path(__file__).parent / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rep.save(str(out_path))
    print(f"  Saved → {out_path}")


if __name__ == "__main__":
    main()
