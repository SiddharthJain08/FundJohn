"""
bt_shv14.py — Backtest harness for S-HV14 OTM Put Skew Factor.

Validates Xing/Zhang/Zhao (2010) smirk signal on FundJohn data.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_framework import (
    Backtester, BacktestConfig, EquityTrade,
    gen_synthetic_prices, gen_synthetic_options,
)

VPS_DATA_DIR = Path("/root/openclaw/data/master")


def load_real_data(start: str = "2018-01-01"):
    px = pd.read_parquet(VPS_DATA_DIR / "prices.parquet")
    opt = pd.read_parquet(VPS_DATA_DIR / "options_eod.parquet")
    px['date'] = pd.to_datetime(px['date']).dt.date
    opt['date'] = pd.to_datetime(opt['date']).dt.date
    opt['expiry'] = pd.to_datetime(opt['expiry']).dt.date
    px = px[px['date'] >= pd.to_datetime(start).date()]
    opt = opt[opt['date'] >= pd.to_datetime(start).date()]
    return px, opt


def load_synthetic_data(seed: int = 42):
    px = gen_synthetic_prices(n_days=2520, n_tickers=40, seed=seed)
    opt = gen_synthetic_options(px, seed=seed)
    return px, opt


def compute_smirk_per_ticker(opt_snap: pd.DataFrame) -> pd.DataFrame:
    """smirk = iv_otm_put(|delta|≈0.20) − iv_atm_call(|delta|≈0.50)
    Computed on the nearest expiry in [20, 60] DTE per ticker.
    """
    if opt_snap.empty:
        return pd.DataFrame()
    s = opt_snap.copy()
    s['dte'] = (pd.to_datetime(s['expiry']) - pd.to_datetime(s['date'])).dt.days
    s = s[s['dte'].between(20, 60) & (s['open_interest'] >= 50)]
    if s.empty:
        return pd.DataFrame()

    s['rank'] = s.groupby('ticker')['dte'].rank('dense', ascending=True)
    s = s[s['rank'] == 1]

    puts = s[(s['option_type'] == 'put') & s['delta'].abs().between(0.15, 0.25)]
    calls = s[(s['option_type'] == 'call') & s['delta'].abs().between(0.45, 0.55)]
    if puts.empty or calls.empty:
        return pd.DataFrame()

    p = puts.groupby('ticker')['implied_volatility'].mean().rename('iv_otm_put')
    c = calls.groupby('ticker')['implied_volatility'].mean().rename('iv_atm_call')
    df = pd.concat([p, c], axis=1).dropna()
    df['smirk'] = df['iv_otm_put'] - df['iv_atm_call']
    return df.reset_index()


def run_backtest(px: pd.DataFrame, opt: pd.DataFrame,
                 hold_days: int = 10,
                 rebalance: str = "W-FRI",
                 top_n_per_side: int = 5,
                 smirk_short_min: float = 0.05,
                 smirk_long_max: float = 0.005) -> dict:
    px['date'] = pd.to_datetime(px['date'])
    px_wide = px.pivot(index='date', columns='ticker', values='close').sort_index()
    opt['date'] = pd.to_datetime(opt['date'])

    rebal_dates = pd.date_range(px_wide.index.min(), px_wide.index.max(), freq=rebalance)
    rebal_dates = [d for d in rebal_dates if d in px_wide.index]

    bt = Backtester(BacktestConfig(annualisation=252, fee_bps=2.0),
                    name="S-HV14_otm_skew_factor")
    n_rebal, n_trades = 0, 0
    for d in rebal_dates:
        snap = opt[opt['date'] == d]
        if snap.empty:
            continue
        sm = compute_smirk_per_ticker(snap)
        if sm.empty or len(sm) < 2 * top_n_per_side:
            continue

        shorts = sm[sm['smirk'] >= smirk_short_min].nlargest(top_n_per_side, 'smirk')
        longs = sm[sm['smirk'] <= smirk_long_max].nsmallest(top_n_per_side, 'smirk')

        d_idx = px_wide.index.get_loc(d)
        exit_idx = min(d_idx + hold_days, len(px_wide) - 1)
        d_exit = px_wide.index[exit_idx]
        weight_per_side = 0.5 / max(len(longs) + len(shorts), 1)

        for _, r in longs.iterrows():
            tk = r['ticker']
            if tk not in px_wide.columns:
                continue
            entry, exit_p = px_wide[tk].loc[d], px_wide[tk].loc[d_exit]
            if pd.isna(entry) or pd.isna(exit_p):
                continue
            bt.add_trade(EquityTrade(
                ticker=tk, entry_date=d.date(), exit_date=d_exit.date(),
                direction='LONG', entry_price=float(entry), exit_price=float(exit_p),
                weight=float(weight_per_side), label='S-HV14',
            ))
            n_trades += 1

        for _, r in shorts.iterrows():
            tk = r['ticker']
            if tk not in px_wide.columns:
                continue
            entry, exit_p = px_wide[tk].loc[d], px_wide[tk].loc[d_exit]
            if pd.isna(entry) or pd.isna(exit_p):
                continue
            bt.add_trade(EquityTrade(
                ticker=tk, entry_date=d.date(), exit_date=d_exit.date(),
                direction='SHORT', entry_price=float(entry), exit_price=float(exit_p),
                weight=float(weight_per_side), label='S-HV14',
            ))
            n_trades += 1
        n_rebal += 1

    return {'report': bt.report(), 'n_rebalances': n_rebal, 'n_trades_total': n_trades}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--real', action='store_true')
    p.add_argument('--out', type=str, default='results/bt_shv14_synthetic.json')
    args = p.parse_args()

    if args.real and VPS_DATA_DIR.exists():
        px, opt = load_real_data(); mode = 'real'
    else:
        px, opt = load_synthetic_data(); mode = 'synthetic'

    print(f"S-HV14 backtest — mode={mode}")
    print(f"  prices  : {len(px):,} rows, {px['ticker'].nunique()} tickers")
    print(f"  options : {len(opt):,} rows, {opt['ticker'].nunique()} tickers")

    out = run_backtest(px, opt)
    rep = out['report']
    print(f"\n  n_rebalances : {out['n_rebalances']}")
    print(f"  n_trades     : {out['n_trades_total']}")
    print(f"  Sharpe       : {rep.sharpe:.3f}  (IS {rep.is_sharpe:.3f}  OOS {rep.oos_sharpe:.3f})")
    print(f"  Max DD       : {rep.max_dd*100:.1f}%   Win rate: {rep.win_rate*100:.1f}%")
    print(f"  Cum/Ann ret  : {rep.cum_return*100:.1f}% / {rep.annualised_return*100:.1f}%")

    out_path = Path(__file__).parent / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rep.save(str(out_path))
    print(f"\n  Saved → {out_path}")


if __name__ == "__main__":
    main()
