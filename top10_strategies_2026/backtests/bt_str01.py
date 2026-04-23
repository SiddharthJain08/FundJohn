"""
bt_str01.py — Backtest harness for S-TR-01 VVIX Early Warning.

Evaluates as a regime classifier: hit-rate of "VIX spikes ≥ 30% within
30 trading days" conditional on a VVIX_pct_252d ≤ 15 fire.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_framework import (
    Backtester, BacktestConfig, RegimeEvent,
)

VPS_DATA_DIR = Path("/root/openclaw/data/master")
VOL_PARQ = VPS_DATA_DIR / "vol_indices.parquet"


def load_real_vol():
    if not VOL_PARQ.exists():
        return None
    v = pd.read_parquet(VOL_PARQ)
    v['date'] = pd.to_datetime(v['date'])
    return v.sort_values('date')


def gen_synthetic_vol(n_days: int = 2520, seed: int = 7) -> pd.DataFrame:
    """Generate stylised VIX/VVIX time series with embedded vol clustering &
    regime spikes — matches qualitative behavior of CBOE VIX/VVIX index."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2016-01-01', periods=n_days, freq='B')

    # VIX: log-AR(1) around log-mean ~2.85 (≈ 17), with stochastic vol
    log_v = np.zeros(n_days)
    log_v[0] = np.log(17.0)
    for t in range(1, n_days):
        shock = rng.normal(0, 0.06)
        # Occasional spike events (~ 4/yr)
        if rng.random() < 0.015:
            shock += rng.normal(0.30, 0.10)
        log_v[t] = 0.95 * log_v[t-1] + 0.05 * np.log(17.0) + shock
    vix = np.exp(log_v)

    # VVIX: log-AR(1) around 88 with positive correlation to VIX changes
    log_vv = np.zeros(n_days)
    log_vv[0] = np.log(90.0)
    dlog_vix = np.diff(log_v, prepend=log_v[0])
    for t in range(1, n_days):
        shock = rng.normal(0, 0.04)
        log_vv[t] = (0.90 * log_vv[t-1] + 0.10 * np.log(90.0)
                     + 0.30 * dlog_vix[t] + shock)
    vvix = np.exp(log_vv)

    return pd.DataFrame({'date': dates, 'vix_close': vix, 'vvix_close': vvix})


def run_backtest(vol: pd.DataFrame,
                 pct_threshold: float = 15.0,
                 horizon_days: int = 30,
                 cooldown_days: int = 30,
                 vix_spike_pct: float = 0.30) -> dict:
    vol = vol.sort_values('date').reset_index(drop=True)
    vol['vvix_pct_252d'] = vol['vvix_close'].rolling(252, min_periods=30) \
                                            .rank(pct=True) * 100
    bt = Backtester(BacktestConfig(annualisation=252),
                    name='S-TR01_vvix_early_warning')

    last_fire = -10**9
    n_fire = 0
    n_hit = 0

    for i in range(len(vol)):
        if pd.isna(vol.at[i, 'vvix_pct_252d']):
            continue
        if vol.at[i, 'vvix_pct_252d'] > pct_threshold:
            continue
        if i - last_fire < cooldown_days:
            continue
        # Fire!
        last_fire = i
        n_fire += 1
        # Evaluate hit: max VIX in next horizon_days vs entry VIX
        end = min(i + horizon_days, len(vol) - 1)
        max_future = vol['vix_close'].iloc[i+1:end+1].max() if end > i else vol.at[i, 'vix_close']
        entry_vix = vol.at[i, 'vix_close']
        hit = (max_future / entry_vix - 1.0) >= vix_spike_pct
        if hit:
            n_hit += 1
        # Forward return on VIX (proxy for hedge P&L) over the horizon
        fwd_ret = float(max_future / entry_vix - 1.0) if entry_vix > 0 else 0.0
        bt.add_trade(RegimeEvent(
            fire_date=vol.at[i, 'date'].date(),
            horizon_days=horizon_days,
            target_event_realised=bool(hit),
            forward_return=fwd_ret,
            label='S-TR01',
        ))

    hit_rate = (n_hit / max(n_fire, 1))
    rep = bt.report()
    return {'report': rep, 'n_fires': n_fire, 'n_hits': n_hit,
            'hit_rate': hit_rate}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--real', action='store_true')
    p.add_argument('--out', type=str, default='results/bt_str01_synthetic.json')
    args = p.parse_args()

    if args.real and VOL_PARQ.exists():
        vol = load_real_vol(); mode = 'real'
    else:
        vol = gen_synthetic_vol(); mode = 'synthetic'

    print(f"S-TR01 backtest — mode={mode}")
    print(f"  vol obs : {len(vol):,}")
    out = run_backtest(vol)
    print(f"\n  n_fires  : {out['n_fires']}")
    print(f"  n_hits   : {out['n_hits']}")
    print(f"  hit_rate : {out['hit_rate']*100:.1f}%  (target ≥ 30%)")
    rep = out['report']
    print(f"  Sharpe   : {rep.sharpe:.3f}  (regime-event mode)")

    out_path = Path(__file__).parent / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rep.save(str(out_path))
    print(f"\n  Saved → {out_path}")


if __name__ == "__main__":
    main()
