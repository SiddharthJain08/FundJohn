"""
bt_shv20.py — Backtest harness for S-HV20 IV Dispersion Reversion.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_framework import (
    Backtester, BacktestConfig, OptionsVolTrade,
    gen_synthetic_prices, gen_synthetic_options, realised_vol,
)

VPS_DATA_DIR = Path("/root/openclaw/data/master")


def load_real_data():
    px = pd.read_parquet(VPS_DATA_DIR / "prices.parquet")
    opt = pd.read_parquet(VPS_DATA_DIR / "options_eod.parquet")
    px['date'] = pd.to_datetime(px['date']).dt.date
    opt['date'] = pd.to_datetime(opt['date']).dt.date
    opt['expiry'] = pd.to_datetime(opt['expiry']).dt.date
    return px, opt


def load_synthetic_data(seed: int = 42):
    px = gen_synthetic_prices(n_days=2520, n_tickers=40, seed=seed)
    opt = gen_synthetic_options(px, seed=seed)
    return px, opt


def daily_atm_iv_30d(opt: pd.DataFrame) -> pd.DataFrame:
    """Per (ticker, date) ATM IV at the expiry closest to 30 DTE."""
    s = opt.copy()
    s['date'] = pd.to_datetime(s['date'])
    s['expiry'] = pd.to_datetime(s['expiry'])
    s['dte'] = (s['expiry'] - s['date']).dt.days
    s = s[s['delta'].abs().between(0.40, 0.60) & (s['dte'].between(20, 45))
          & (s['open_interest'] >= 50)]
    g = s.groupby(['ticker', 'date', 'dte'])['implied_volatility'].mean().reset_index()
    # Pick the per-(ticker,date) row with dte closest to 30
    g['rank_diff'] = (g['dte'] - 30).abs()
    g = g.sort_values(['ticker', 'date', 'rank_diff']).drop_duplicates(['ticker', 'date'])
    return g[['ticker', 'date', 'implied_volatility']].rename(
        columns={'implied_volatility': 'atm_iv_30d'})


def compute_dispersion_series(opt: pd.DataFrame,
                              index_ticker: str = 'SPY',
                              window: int = 252) -> pd.DataFrame:
    """daily iv_dispersion = mean(component_atm_iv_30d) / spx_atm_iv_30d
    Plus its rolling-window percentile."""
    iv = daily_atm_iv_30d(opt)
    if iv.empty:
        return pd.DataFrame()

    if index_ticker not in iv['ticker'].unique():
        # Use first ticker as proxy index
        index_ticker = iv['ticker'].iloc[0]

    spx = iv[iv['ticker'] == index_ticker].set_index('date')['atm_iv_30d']
    comps = iv[iv['ticker'] != index_ticker]
    comp_mean = comps.groupby('date')['atm_iv_30d'].mean()

    df = pd.concat([spx.rename('spx_iv'), comp_mean.rename('comp_iv')], axis=1).dropna()
    df['dispersion'] = df['comp_iv'] / df['spx_iv']
    df['pct_252d'] = df['dispersion'].rolling(window, min_periods=30) \
                                     .rank(pct=True) * 100
    return df.reset_index()


def run_backtest(px, opt, hold_days: int = 10,
                 low_pct: float = 10.0, high_pct: float = 90.0,
                 basket_n: int = 5) -> dict:
    px['date'] = pd.to_datetime(px['date'])
    px_wide = px.pivot(index='date', columns='ticker', values='close').sort_index()

    iv_daily = daily_atm_iv_30d(opt)
    iv_daily['date'] = pd.to_datetime(iv_daily['date'])
    iv_pivot = iv_daily.pivot(index='date', columns='ticker', values='atm_iv_30d')

    disp = compute_dispersion_series(opt)
    if disp.empty:
        return {'report': Backtester(BacktestConfig()).report(), 'n_trades_total': 0}
    disp['date'] = pd.to_datetime(disp['date'])
    disp = disp.set_index('date')

    bt = Backtester(BacktestConfig(annualisation=252,
                                   options_commission_per_contract=0.65,
                                   options_slippage_bps=10.0),
                    name='S-HV20_iv_dispersion_reversion')

    n_trades = 0
    # Trigger on every disp-snapshot date that is also a trading day
    trigger_dates = [d for d in disp.index if d in px_wide.index]

    for d in trigger_dates:
        if d not in px_wide.index:
            continue
        d_idx = px_wide.index.get_loc(d)
        exit_idx = min(d_idx + hold_days, len(px_wide) - 1)
        d_exit = px_wide.index[exit_idx]
        tau = max(1, (d_exit - d).days) / 365.0

        pct = disp.loc[d, 'pct_252d']
        if pd.isna(pct):
            continue

        if pct < low_pct:
            # Short index vol on SPY (or proxy first ticker)
            tk = 'SPY' if 'SPY' in px_wide.columns else px_wide.columns[0]
            S0, S1 = px_wide[tk].loc[d], px_wide[tk].loc[d_exit]
            if pd.isna(S0) or pd.isna(S1):
                continue
            iv_entry = float(iv_pivot[tk].loc[d]) if (tk in iv_pivot.columns and
                                                     d in iv_pivot.index) else 0.18
            actual_ret = abs(S1 / S0 - 1)
            rv = actual_ret / np.sqrt(tau)
            bt.add_trade(OptionsVolTrade(
                ticker=tk, entry_date=d.date(), exit_date=d_exit.date(),
                direction='SELL_VOL', iv_entry=iv_entry,
                rv_realised=float(rv), tau_years=float(tau),
                weight=0.015, label='S-HV20-INDEX',
            ))
            n_trades += 1

        elif pct > high_pct:
            # Short basket of top-N highest-IV component
            ivs = iv_pivot.loc[d].dropna()
            tk_anchor = 'SPY' if 'SPY' in ivs.index else ivs.index[0]
            ivs = ivs.drop(tk_anchor, errors='ignore')
            ivs = ivs.nlargest(basket_n)
            for tk, iv_entry in ivs.items():
                if tk not in px_wide.columns:
                    continue
                S0, S1 = px_wide[tk].loc[d], px_wide[tk].loc[d_exit]
                if pd.isna(S0) or pd.isna(S1):
                    continue
                actual_ret = abs(S1 / S0 - 1)
                rv = actual_ret / np.sqrt(tau)
                bt.add_trade(OptionsVolTrade(
                    ticker=tk, entry_date=d.date(), exit_date=d_exit.date(),
                    direction='SELL_VOL', iv_entry=float(iv_entry),
                    rv_realised=float(rv), tau_years=float(tau),
                    weight=0.005, label='S-HV20-BASKET',
                ))
                n_trades += 1

    return {'report': bt.report(), 'n_trades_total': n_trades}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--real', action='store_true')
    p.add_argument('--out', type=str, default='results/bt_shv20_synthetic.json')
    args = p.parse_args()

    if args.real and VPS_DATA_DIR.exists():
        px, opt = load_real_data(); mode = 'real'
    else:
        px, opt = load_synthetic_data(); mode = 'synthetic'

    print(f"S-HV20 backtest — mode={mode}")
    print(f"  prices  : {len(px):,} rows    options : {len(opt):,}")
    out = run_backtest(px, opt)
    rep = out['report']
    print(f"\n  n_trades : {out['n_trades_total']}")
    print(f"  Sharpe   : {rep.sharpe:.3f}  (IS {rep.is_sharpe:.3f}  OOS {rep.oos_sharpe:.3f})")
    print(f"  Max DD   : {rep.max_dd*100:.1f}%   Win: {rep.win_rate*100:.1f}%")
    print(f"  Cum/Ann  : {rep.cum_return*100:.1f}% / {rep.annualised_return*100:.1f}%")

    out_path = Path(__file__).parent / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rep.save(str(out_path))
    print(f"\n  Saved → {out_path}")


if __name__ == "__main__":
    main()
