"""
bt_str02.py — Backtest harness for S-TR-02 Hurst Regime Flip.

Hit definition: a regime flip fire is a HIT if SPY 21-day forward
realised vol exceeds the trailing 60-day average by ≥ 30%.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_framework import (
    Backtester, BacktestConfig, RegimeEvent, gen_synthetic_prices,
)

# Import the Hurst R/S estimator from the implementation file
sys.path.insert(0, str(Path(__file__).parent.parent / 'implementations'))
from str02_hurst_regime_flip import hurst_rs

VPS_DATA_DIR = Path("/root/openclaw/data/master")


def load_real_spy() -> pd.Series | None:
    p = VPS_DATA_DIR / "prices.parquet"
    if not p.exists():
        return None
    px = pd.read_parquet(p)
    spy = px[px['ticker'] == 'SPY'].sort_values('date')
    if spy.empty:
        return None
    spy['date'] = pd.to_datetime(spy['date'])
    return spy.set_index('date')['close']


def synthetic_spy(seed: int = 42) -> pd.Series:
    px = gen_synthetic_prices(n_days=2520, n_tickers=1, seed=seed)
    px['date'] = pd.to_datetime(px['date'])
    return px.set_index('date')['close']


def run_backtest(spy: pd.Series,
                 window: int = 60,
                 h_trend_min: float = 0.55,
                 h_revert_max: float = 0.45,
                 cooldown_days: int = 20,
                 fwd_horizon_days: int = 21,
                 fwd_vol_jump_threshold: float = 0.30) -> dict:
    spy = spy.sort_index().dropna()
    log_ret = np.log(spy).diff().dropna().values
    dates = spy.index[1:]   # aligned with log_ret

    bt = Backtester(BacktestConfig(annualisation=252),
                    name='S-TR02_hurst_regime_flip')
    last_fire = -10**9
    n_fire, n_hit = 0, 0

    # Pre-compute rolling realised vol for hit definition
    rv60 = pd.Series(log_ret).rolling(60).std() * np.sqrt(252)

    for i in range(window + 20, len(log_ret) - fwd_horizon_days - 1):
        if i - last_fire < cooldown_days:
            continue
        H_now = hurst_rs(log_ret[i-window:i])
        H_prev = hurst_rs(log_ret[i-window-15:i-15])
        if not (H_prev >= h_trend_min and H_now <= h_revert_max):
            continue
        # Fire
        last_fire = i
        n_fire += 1
        rv_baseline = rv60.iloc[i] if not pd.isna(rv60.iloc[i]) else 0.18
        fwd_window = log_ret[i+1:i+1+fwd_horizon_days]
        rv_fwd = float(np.std(fwd_window) * np.sqrt(252))
        hit = (rv_fwd >= (1 + fwd_vol_jump_threshold) * rv_baseline)
        if hit:
            n_hit += 1
        fwd_ret = float(np.sum(fwd_window))
        bt.add_trade(RegimeEvent(
            fire_date=dates[i].date(),
            horizon_days=fwd_horizon_days,
            target_event_realised=bool(hit),
            forward_return=fwd_ret,
            label='S-TR02',
        ))

    rep = bt.report()
    return {'report': rep, 'n_fires': n_fire, 'n_hits': n_hit,
            'hit_rate': (n_hit / max(n_fire, 1))}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--real', action='store_true')
    p.add_argument('--out', type=str, default='results/bt_str02_synthetic.json')
    args = p.parse_args()

    spy = load_real_spy() if args.real else None
    mode = 'real' if spy is not None else 'synthetic'
    if spy is None:
        spy = synthetic_spy()

    print(f"S-TR02 backtest — mode={mode}")
    print(f"  spy obs : {len(spy):,}")
    out = run_backtest(spy)
    print(f"\n  n_fires  : {out['n_fires']}")
    print(f"  n_hits   : {out['n_hits']}")
    print(f"  hit_rate : {out['hit_rate']*100:.1f}%  (target ≥ 50%)")

    out_path = Path(__file__).parent / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out['report'].save(str(out_path))
    print(f"  Saved → {out_path}")


if __name__ == "__main__":
    main()
