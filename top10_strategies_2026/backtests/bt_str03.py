"""
bt_str03.py — Backtest harness for S-TR-03 BOCPD.

Hit definition: SPY 21-day forward realised vol exceeds trailing 60-day RV
by ≥ 30 % within 21 days of fire.
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

sys.path.insert(0, str(Path(__file__).parent.parent / 'implementations'))
from str03_bocpd import bocpd_run_length

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


def run_backtest(spy: pd.Series, lookback: int = 200,
                 cp_threshold: float = 0.40, cooldown: int = 15,
                 horizon: int = 21, vol_jump: float = 0.30) -> dict:
    spy = spy.sort_index().dropna()
    log_ret = np.log(spy).diff().dropna().values
    dates = spy.index[1:]
    rv60 = pd.Series(log_ret).rolling(60).std() * np.sqrt(252)

    bt = Backtester(BacktestConfig(annualisation=252),
                    name='S-TR03_bocpd')
    last_fire = -10**9
    n_fire, n_hit = 0, 0
    cp_at_fire = []

    # Walk forward, computing BOCPD on a rolling window
    for i in range(lookback, len(log_ret) - horizon - 1):
        if i - last_fire < cooldown:
            continue
        window = log_ret[i-lookback:i]
        std = window.std() or 1.0
        window_n = (window - window.mean()) / std
        cp = bocpd_run_length(window_n, hazard_lambda=30.0)
        if cp[-1] < cp_threshold:
            continue
        last_fire = i
        n_fire += 1
        rv_baseline = rv60.iloc[i] if not pd.isna(rv60.iloc[i]) else 0.18
        fwd = log_ret[i+1:i+1+horizon]
        rv_fwd = float(np.std(fwd) * np.sqrt(252))
        hit = rv_fwd >= (1 + vol_jump) * rv_baseline
        if hit:
            n_hit += 1
        bt.add_trade(RegimeEvent(
            fire_date=dates[i].date(),
            horizon_days=horizon,
            target_event_realised=bool(hit),
            forward_return=float(np.sum(fwd)),
            label='S-TR03',
        ))

    rep = bt.report()
    return {'report': rep, 'n_fires': n_fire, 'n_hits': n_hit,
            'hit_rate': n_hit / max(n_fire, 1)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--real', action='store_true')
    p.add_argument('--out', type=str, default='results/bt_str03_synthetic.json')
    args = p.parse_args()

    spy = load_real_spy() if args.real else None
    mode = 'real' if spy is not None else 'synthetic'
    if spy is None:
        spy = synthetic_spy()

    print(f"S-TR03 backtest — mode={mode}")
    print(f"  spy obs : {len(spy):,}")
    out = run_backtest(spy)
    print(f"\n  n_fires  : {out['n_fires']}")
    print(f"  n_hits   : {out['n_hits']}")
    print(f"  hit_rate : {out['hit_rate']*100:.1f}%  (target ≥ 60%)")

    out_path = Path(__file__).parent / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out['report'].save(str(out_path))
    print(f"  Saved → {out_path}")


if __name__ == "__main__":
    main()
