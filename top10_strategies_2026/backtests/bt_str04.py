"""
bt_str04.py — Backtest harness for S-TR-04 Zarattini Intraday SPY.

Synthetic mode: stylised 30-min SPY bars with mean-reverting noise plus a
small intraday autocorrelation in returns (gap leads in same direction).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from datetime import time, datetime

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_framework import (
    Backtester, BacktestConfig, IntradayTrade,
)

sys.path.insert(0, str(Path(__file__).parent.parent / 'implementations'))
from str04_zarattini_intraday_spy import compute_nope

VPS_DATA_DIR = Path("/root/openclaw/data/master")
PRICES_30M_PARQ = VPS_DATA_DIR / "prices_30m.parquet"


def load_real_30m() -> pd.DataFrame | None:
    if not PRICES_30M_PARQ.exists():
        return None
    df = pd.read_parquet(PRICES_30M_PARQ)
    df = df[df['ticker'] == 'SPY']
    if df.empty:
        return None
    df['datetime'] = pd.to_datetime(df['datetime'])
    return df.sort_values('datetime').reset_index(drop=True)


def gen_synthetic_30m(n_days: int = 1500, seed: int = 99) -> pd.DataFrame:
    """13 30-min bars per session (09:30 → 16:00 ET).  Daily structure:
       gap from yesterday close → today open ~ N(0, 0.5%)
       intraday returns mean-revert with momentum carry from the gap."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2018-01-01', periods=n_days, freq='B')
    bars_per_day = 13
    rows = []
    last_close = 350.0
    for d in dates:
        gap_ret = rng.normal(0.0, 0.005)
        today_open = last_close * (1 + gap_ret)
        # 13 returns: weak persistence with the gap
        intraday_eps = rng.normal(0.0, 0.0015, bars_per_day)
        # Inject autocorrelation with gap_sign
        for k in range(bars_per_day):
            intraday_eps[k] += 0.0004 * np.sign(gap_ret) * (0.7 ** k)
        prices = np.cumprod(1 + intraday_eps) * today_open
        for k in range(bars_per_day):
            ts = pd.Timestamp.combine(d.date(),
                                      time(9, 30)) + pd.Timedelta(minutes=30 * k)
            o = today_open if k == 0 else prices[k-1]
            c = prices[k]
            h = max(o, c) * (1 + abs(rng.normal(0, 0.0003)))
            l = min(o, c) * (1 - abs(rng.normal(0, 0.0003)))
            v = int(rng.integers(800_000, 3_000_000))
            rows.append({'ticker': 'SPY', 'datetime': ts,
                         'open': o, 'high': h, 'low': l, 'close': c,
                         'volume': v})
        last_close = prices[-1]
    return pd.DataFrame(rows)


def run_backtest(bars: pd.DataFrame,
                 nope_long_min: float = 0.20,
                 nope_short_max: float = -0.20,
                 allow_short: bool = False,
                 vix_default: float = 18.0) -> dict:
    bars = bars.copy()
    bars['date'] = bars['datetime'].dt.date
    bars['time'] = bars['datetime'].dt.time

    bt = Backtester(BacktestConfig(annualisation=252, fee_bps=1.0),
                    name='S-TR04_zarattini_intraday')

    # Pre-compute 10-day rolling first-bar volume by date
    first_bar_t = time(9, 30)
    first_bars = bars[bars['time'] == first_bar_t].sort_values('date').copy()
    first_bars['vol_10d'] = first_bars['volume'].rolling(10, min_periods=3).mean()

    daily_groups = list(bars.groupby('date'))
    n_trade = 0
    prev_close = None

    for d, dayb in daily_groups:
        dayb = dayb.sort_values('datetime')
        if dayb.empty or dayb.shape[0] < 2:
            continue
        first_row = dayb.iloc[0]
        last_row = dayb.iloc[-1]
        if prev_close is None:
            prev_close = float(last_row['close'])
            continue

        vol_10d_row = first_bars[first_bars['date'] == d]
        vol_10d = (vol_10d_row['vol_10d'].iloc[0]
                   if not vol_10d_row.empty and not pd.isna(vol_10d_row['vol_10d'].iloc[0])
                   else float(first_row['volume']))

        nope = compute_nope(prev_close=float(prev_close),
                            today_open=float(first_row['open']),
                            first_bar_volume=float(first_row['volume']),
                            vol_10d_avg=float(vol_10d),
                            vix=vix_default)

        direction = None
        if nope >= nope_long_min:
            direction = 'LONG'
        elif allow_short and nope <= nope_short_max:
            direction = 'SHORT'

        if direction:
            entry = float(first_row['open'])
            exit_p = float(last_row['close'])
            bt.add_trade(IntradayTrade(
                ticker='SPY',
                entry_dt=first_row['datetime'],
                exit_dt=last_row['datetime'],
                direction=direction,
                entry_price=entry, exit_price=exit_p,
                weight=0.50,
                label='S-TR04',
            ))
            n_trade += 1

        prev_close = float(last_row['close'])

    return {'report': bt.report(), 'n_trades_total': n_trade}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--real', action='store_true')
    p.add_argument('--out', type=str, default='results/bt_str04_synthetic.json')
    args = p.parse_args()

    if args.real and PRICES_30M_PARQ.exists():
        bars = load_real_30m(); mode = 'real'
    else:
        bars = gen_synthetic_30m(); mode = 'synthetic'

    print(f"S-TR04 backtest — mode={mode}")
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
