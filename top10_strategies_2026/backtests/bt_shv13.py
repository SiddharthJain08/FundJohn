"""
bt_shv13.py — Backtest harness for S-HV13 Call-Put IV Spread.

Validates the Cremers & Weinbaum (2010) signal on FundJohn data.

Run modes
---------
1. VPS mode (default if /root/openclaw/data exists):
       python3 bt_shv13.py
   → loads real options_eod + prices parquet, computes weekly cross-sectional
     iv_spread per ticker, holds 7 days, reports Sharpe / IS / OOS / by-regime.

2. Synthetic mode (no real data):
       python3 bt_shv13.py --synthetic
   → uses gen_synthetic_prices/options for framework correctness check.

Methodology
-----------
For each rebalance date (every Friday close):
1. Load options chain snapshot, filter to ATM (|delta| 0.40-0.60) puts and calls
   with DTE in [10, 45].
2. Match call/put pairs at identical (strike, expiry).
3. Compute OI-weighted iv_spread per ticker.
4. Form long/short basket from top/bottom decile.
5. Hold 5 trading days; mark-to-market using prices.parquet close.
6. Assume 2 bps round-trip equity cost.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_framework import (
    Backtester, BacktestConfig, EquityTrade,
    gen_synthetic_prices, gen_synthetic_options,
)

VPS_DATA_DIR = Path("/root/openclaw/data/master")


# ─── data loaders ────────────────────────────────────────────────────────────

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


# ─── per-rebalance signal generator (mirrors strategy class internals) ──────

def compute_iv_spread_per_ticker(opt_snap: pd.DataFrame) -> pd.DataFrame:
    """Pre-compute per-ticker iv_spread_atm_oi_weighted from a single-date option chain."""
    if opt_snap.empty:
        return pd.DataFrame()

    # Add DTE
    opt_snap = opt_snap.copy()
    opt_snap['dte'] = (
        pd.to_datetime(opt_snap['expiry']) - pd.to_datetime(opt_snap['date'])
    ).dt.days
    # ATM filter
    atm = opt_snap[
        (opt_snap['delta'].abs().between(0.40, 0.60)) &
        (opt_snap['dte'].between(10, 45)) &
        (opt_snap['open_interest'] >= 50)
    ].copy()
    if atm.empty:
        return pd.DataFrame()

    # Pick the single nearest-DTE expiry per ticker
    atm['rank'] = atm.groupby('ticker')['dte'].rank('dense', ascending=True)
    atm = atm[atm['rank'] == 1]

    # Match call/put at same strike per ticker
    calls = atm[atm['option_type'] == 'call'][
        ['ticker', 'strike', 'implied_volatility', 'open_interest']
    ].rename(columns={'implied_volatility': 'iv_call', 'open_interest': 'oi_call'})
    puts = atm[atm['option_type'] == 'put'][
        ['ticker', 'strike', 'implied_volatility', 'open_interest']
    ].rename(columns={'implied_volatility': 'iv_put', 'open_interest': 'oi_put'})
    pairs = calls.merge(puts, on=['ticker', 'strike'])
    if pairs.empty:
        return pd.DataFrame()

    pairs['oi_pair'] = pairs[['oi_call', 'oi_put']].min(axis=1)
    pairs['iv_spread'] = pairs['iv_call'] - pairs['iv_put']

    # OI-weighted average iv_spread per ticker
    result = pairs.groupby('ticker').apply(
        lambda g: pd.Series({
            'iv_spread': float(np.average(g['iv_spread'], weights=g['oi_pair']))
                         if g['oi_pair'].sum() > 0 else float(g['iv_spread'].mean()),
            'oi_total': float(g['oi_pair'].sum()),
        })
    ).reset_index()
    return result


def build_iv_rank_table(opt_df: pd.DataFrame, lookback: int = 252) -> pd.DataFrame:
    """For each (ticker, date), iv_rank ≈ percentile of ATM IV vs trailing 252d.

    Used for confidence scoring downstream.
    """
    atm = opt_df[opt_df['delta'].abs().between(0.40, 0.60)].copy()
    if atm.empty:
        return pd.DataFrame()
    daily_atm_iv = atm.groupby(['ticker', 'date'])['implied_volatility'].mean().reset_index()
    daily_atm_iv.rename(columns={'implied_volatility': 'atm_iv'}, inplace=True)
    daily_atm_iv['date'] = pd.to_datetime(daily_atm_iv['date'])
    daily_atm_iv = daily_atm_iv.sort_values(['ticker', 'date'])
    daily_atm_iv['iv_rank'] = daily_atm_iv.groupby('ticker')['atm_iv'].transform(
        lambda s: s.rolling(lookback, min_periods=20).rank(pct=True) * 100
    )
    return daily_atm_iv


# ─── backtest ───────────────────────────────────────────────────────────────

def run_backtest(px: pd.DataFrame, opt: pd.DataFrame,
                 hold_days: int = 5,
                 rebalance: str = "W-FRI",
                 top_n_per_side: int = 5,
                 spread_long_min: float = 0.025,
                 spread_short_max: float = -0.025) -> dict:

    px['date'] = pd.to_datetime(px['date'])
    px = px.sort_values(['ticker', 'date'])
    px_wide = px.pivot(index='date', columns='ticker', values='close')

    opt['date'] = pd.to_datetime(opt['date'])
    iv_rank_table = build_iv_rank_table(opt)

    # Compute regime per day from SPY (or first ticker as fallback)
    regime_anchor = 'SPY' if 'SPY' in px_wide.columns else px_wide.columns[0]
    spy_ret = px_wide[regime_anchor].pct_change()
    rv20 = spy_ret.rolling(20).std() * np.sqrt(252)
    regime = pd.Series('NEUTRAL', index=spy_ret.index)
    regime[rv20 > 0.25] = 'HIGH_VOL'
    regime[rv20 < 0.12] = 'LOW_VOL'

    # Rebalance dates
    rebal_dates = pd.date_range(px_wide.index.min(), px_wide.index.max(), freq=rebalance)
    rebal_dates = [d for d in rebal_dates if d in px_wide.index]

    bt = Backtester(BacktestConfig(annualisation=252, fee_bps=2.0),
                    name="S-HV13_call_put_iv_spread")

    n_rebal = 0
    n_trades = 0
    for d in rebal_dates:
        snap = opt[opt['date'] == d]
        if snap.empty:
            continue
        spreads = compute_iv_spread_per_ticker(snap)
        if spreads.empty or len(spreads) < 2 * top_n_per_side:
            continue

        regime_today = regime.loc[d] if d in regime.index else 'NEUTRAL'
        sl = spread_long_min + (0.015 if regime_today == 'HIGH_VOL' else 0.0)
        ss = spread_short_max - (0.015 if regime_today == 'HIGH_VOL' else 0.0)

        longs = spreads[spreads['iv_spread'] >= sl].nlargest(top_n_per_side, 'iv_spread')
        shorts = spreads[spreads['iv_spread'] <= ss].nsmallest(top_n_per_side, 'iv_spread')

        # Mark-to-market: hold for hold_days trading days
        d_idx = px_wide.index.get_loc(d)
        exit_idx = min(d_idx + hold_days, len(px_wide) - 1)
        d_exit = px_wide.index[exit_idx]

        weight_per_side = 0.5 / max(len(longs) + len(shorts), 1)

        for _, r in longs.iterrows():
            tk = r['ticker']
            if tk not in px_wide.columns:
                continue
            entry_p = px_wide[tk].loc[d]
            exit_p = px_wide[tk].loc[d_exit]
            if pd.isna(entry_p) or pd.isna(exit_p):
                continue
            bt.add_trade(EquityTrade(
                ticker=tk, entry_date=d.date(), exit_date=d_exit.date(),
                direction='LONG', entry_price=float(entry_p),
                exit_price=float(exit_p), weight=float(weight_per_side),
                label='S-HV13', regime=str(regime_today),
            ))
            n_trades += 1

        for _, r in shorts.iterrows():
            tk = r['ticker']
            if tk not in px_wide.columns:
                continue
            entry_p = px_wide[tk].loc[d]
            exit_p = px_wide[tk].loc[d_exit]
            if pd.isna(entry_p) or pd.isna(exit_p):
                continue
            bt.add_trade(EquityTrade(
                ticker=tk, entry_date=d.date(), exit_date=d_exit.date(),
                direction='SHORT', entry_price=float(entry_p),
                exit_price=float(exit_p), weight=float(weight_per_side),
                label='S-HV13', regime=str(regime_today),
            ))
            n_trades += 1

        n_rebal += 1

    rep = bt.report()
    return {
        'report': rep,
        'n_rebalances': n_rebal,
        'n_trades_total': n_trades,
    }


# ─── main ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--synthetic', action='store_true', default=True,
                   help='Run on synthetic data (default; auto-fallback if no VPS data)')
    p.add_argument('--real', action='store_true',
                   help='Use real /root/openclaw/data/master parquets if available')
    p.add_argument('--out', type=str,
                   default='results/bt_shv13_synthetic.json')
    args = p.parse_args()

    if args.real and VPS_DATA_DIR.exists():
        px, opt = load_real_data()
        mode = 'real'
    else:
        px, opt = load_synthetic_data()
        mode = 'synthetic'

    print(f"S-HV13 backtest — mode={mode}")
    print(f"  prices  : {len(px):,} rows, {px['ticker'].nunique()} tickers")
    print(f"  options : {len(opt):,} rows, {opt['ticker'].nunique()} tickers")

    out = run_backtest(px, opt)
    rep = out['report']

    print(f"\n  n_rebalances : {out['n_rebalances']}")
    print(f"  n_trades     : {out['n_trades_total']}")
    print(f"  Sharpe       : {rep.sharpe:.3f}")
    print(f"  Sharpe (IS)  : {rep.is_sharpe:.3f}")
    print(f"  Sharpe (OOS) : {rep.oos_sharpe:.3f}")
    print(f"  Sharpe 95%CI : [{rep.sharpe_ci_low:.3f}, {rep.sharpe_ci_high:.3f}]")
    print(f"  Max DD       : {rep.max_dd*100:.1f}%")
    print(f"  Win rate     : {rep.win_rate*100:.1f}%")
    print(f"  Profit factor: {rep.profit_factor:.2f}")
    print(f"  Cum return   : {rep.cum_return*100:.1f}%")
    print(f"  Ann return   : {rep.annualised_return*100:.1f}%")

    out_path = Path(__file__).parent / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rep.save(str(out_path))
    print(f"\n  Saved → {out_path}")


if __name__ == "__main__":
    main()
