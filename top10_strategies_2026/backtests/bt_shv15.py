"""
bt_shv15.py — Backtest harness for S-HV15 IV Term Structure Slope.
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
    return px[px['date'] >= pd.to_datetime(start).date()], \
           opt[opt['date'] >= pd.to_datetime(start).date()]


def load_synthetic_data(seed: int = 42):
    px = gen_synthetic_prices(n_days=2520, n_tickers=40, seed=seed)
    opt = gen_synthetic_options(px, seed=seed)
    return px, opt


def compute_ts_ratio_per_ticker(opt_snap: pd.DataFrame) -> pd.DataFrame:
    """ts_ratio = atm_iv(~30 DTE) / atm_iv(~90 DTE) per ticker."""
    if opt_snap.empty:
        return pd.DataFrame()
    s = opt_snap.copy()
    s['dte'] = (pd.to_datetime(s['expiry']) - pd.to_datetime(s['date'])).dt.days
    s = s[s['delta'].abs().between(0.40, 0.60) & (s['open_interest'] >= 50)]
    if s.empty:
        return pd.DataFrame()

    def closest(group, target):
        idx = (group['dte'] - target).abs().idxmin()
        return group.loc[idx]

    rows = []
    for tk, g in s.groupby('ticker'):
        g30_dte = g.loc[(g['dte'] - 30).abs().idxmin(), 'dte'] if not g.empty else None
        g90_dte = g.loc[(g['dte'] - 90).abs().idxmin(), 'dte'] if not g.empty else None
        if g30_dte is None or g90_dte is None:
            continue
        iv_30 = g[g['dte'] == g30_dte]['implied_volatility'].mean()
        iv_90 = g[g['dte'] == g90_dte]['implied_volatility'].mean()
        if pd.isna(iv_30) or pd.isna(iv_90) or iv_90 <= 0:
            continue
        rows.append({'ticker': tk, 'iv_30d': float(iv_30), 'iv_90d': float(iv_90),
                     'ts_ratio': float(iv_30) / float(iv_90)})
    return pd.DataFrame(rows)


def run_backtest(px, opt,
                 hold_days: int = 7,
                 rebalance: str = 'W-FRI',
                 ts_inv_lo: float = 1.05, ts_inv_hi: float = 1.15,
                 ts_contango: float = 0.85,
                 top_n: int = 6) -> dict:
    px['date'] = pd.to_datetime(px['date'])
    px_wide = px.pivot(index='date', columns='ticker', values='close').sort_index()
    opt['date'] = pd.to_datetime(opt['date'])

    rebal_dates = [d for d in pd.date_range(px_wide.index.min(), px_wide.index.max(),
                                            freq=rebalance) if d in px_wide.index]
    bt = Backtester(BacktestConfig(annualisation=252, fee_bps=2.0),
                    name='S-HV15_iv_term_structure')
    n_rebal, n_trades = 0, 0
    for d in rebal_dates:
        snap = opt[opt['date'] == d]
        if snap.empty:
            continue
        ts = compute_ts_ratio_per_ticker(snap)
        if ts.empty:
            continue

        longs = ts[(ts['ts_ratio'] >= ts_inv_lo) & (ts['ts_ratio'] <= ts_inv_hi)] \
                  .nlargest(top_n, 'ts_ratio')
        shorts = ts[ts['ts_ratio'] <= ts_contango].nsmallest(top_n, 'ts_ratio')

        d_idx = px_wide.index.get_loc(d)
        exit_idx = min(d_idx + hold_days, len(px_wide) - 1)
        d_exit = px_wide.index[exit_idx]
        wt = 0.5 / max(len(longs) + len(shorts), 1)

        for _, r in longs.iterrows():
            tk = r['ticker']
            if tk not in px_wide.columns:
                continue
            ep, xp = px_wide[tk].loc[d], px_wide[tk].loc[d_exit]
            if pd.isna(ep) or pd.isna(xp):
                continue
            bt.add_trade(EquityTrade(
                ticker=tk, entry_date=d.date(), exit_date=d_exit.date(),
                direction='LONG', entry_price=float(ep), exit_price=float(xp),
                weight=float(wt), label='S-HV15',
            ))
            n_trades += 1
        for _, r in shorts.iterrows():
            tk = r['ticker']
            if tk not in px_wide.columns:
                continue
            ep, xp = px_wide[tk].loc[d], px_wide[tk].loc[d_exit]
            if pd.isna(ep) or pd.isna(xp):
                continue
            bt.add_trade(EquityTrade(
                ticker=tk, entry_date=d.date(), exit_date=d_exit.date(),
                direction='SHORT', entry_price=float(ep), exit_price=float(xp),
                weight=float(wt), label='S-HV15',
            ))
            n_trades += 1
        n_rebal += 1

    return {'report': bt.report(), 'n_rebalances': n_rebal, 'n_trades_total': n_trades}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--real', action='store_true')
    p.add_argument('--out', type=str, default='results/bt_shv15_synthetic.json')
    args = p.parse_args()

    if args.real and VPS_DATA_DIR.exists():
        px, opt = load_real_data(); mode = 'real'
    else:
        px, opt = load_synthetic_data(); mode = 'synthetic'

    print(f"S-HV15 backtest — mode={mode}")
    print(f"  prices  : {len(px):,} rows, {px['ticker'].nunique()} tickers")
    print(f"  options : {len(opt):,} rows")

    out = run_backtest(px, opt)
    rep = out['report']
    print(f"\n  n_rebalances : {out['n_rebalances']}    n_trades : {out['n_trades_total']}")
    print(f"  Sharpe       : {rep.sharpe:.3f}  (IS {rep.is_sharpe:.3f}  OOS {rep.oos_sharpe:.3f})")
    print(f"  Max DD       : {rep.max_dd*100:.1f}%   Win rate: {rep.win_rate*100:.1f}%")
    print(f"  Cum/Ann ret  : {rep.cum_return*100:.1f}% / {rep.annualised_return*100:.1f}%")

    out_path = Path(__file__).parent / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rep.save(str(out_path))
    print(f"\n  Saved → {out_path}")


if __name__ == "__main__":
    main()
