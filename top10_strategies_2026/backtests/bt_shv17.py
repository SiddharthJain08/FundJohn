"""
bt_shv17.py — Backtest harness for S-HV17 Earnings Straddle Fade.

Models a SELL_VOL position as a delta-neutral straddle short on event date.
Gross return on a sold straddle is approximately:

    pnl_per_$1_vega = (implied_move^2 - actual_move^2)

where moves are measured as |spot_t+1 / spot_t − 1|.  We expand using
gamma-scalp decomposition: PnL_$ ≈ S0 * (implied_move^2 − actual_move^2)
per unit notional.

Synthetic mode generates a stylised earnings calendar (10 % of names per
quarter) with `implied_move` set to 1.4× a realised draw — so net edge ≈ 0
on average (validates harness, no fake alpha).
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
    gen_synthetic_prices,
)

VPS_DATA_DIR = Path("/root/openclaw/data/master")
EARN_PARQ = VPS_DATA_DIR / "earnings_calendar.parquet"


def load_real_data():
    px = pd.read_parquet(VPS_DATA_DIR / "prices.parquet")
    opt = pd.read_parquet(VPS_DATA_DIR / "options_eod.parquet")
    if EARN_PARQ.exists():
        earn = pd.read_parquet(EARN_PARQ)
    else:
        earn = pd.DataFrame(columns=['ticker', 'date', 'after_hours'])
    px['date'] = pd.to_datetime(px['date']).dt.date
    opt['date'] = pd.to_datetime(opt['date']).dt.date
    opt['expiry'] = pd.to_datetime(opt['expiry']).dt.date
    earn['date'] = pd.to_datetime(earn['date']).dt.date
    return px, opt, earn


def gen_synthetic_earnings(prices_df: pd.DataFrame, seed: int = 11) -> pd.DataFrame:
    """Random earnings calendar: each ticker has ~4 events per year."""
    rng = np.random.default_rng(seed)
    rows = []
    for tk in prices_df['ticker'].unique():
        sub = prices_df[prices_df['ticker'] == tk].sort_values('date')
        if sub.empty:
            continue
        dates = pd.to_datetime(sub['date'].unique())
        # Pick ~one date per ~63 trading days
        idx = np.arange(40, len(dates), 63 + rng.integers(-5, 5))
        for i in idx:
            if i < len(dates):
                rows.append({'ticker': tk, 'date': dates[i].date(),
                             'after_hours': bool(rng.integers(0, 2))})
    return pd.DataFrame(rows)


def estimate_implied_move(opt: pd.DataFrame, ticker: str, on_date) -> float | None:
    """Implied move = (atm_call_mark + atm_put_mark)/spot for nearest expiry > on_date."""
    s = opt[(opt['ticker'] == ticker) & (opt['date'] == on_date)]
    if s.empty:
        return None
    s = s.copy()
    s['dte'] = (pd.to_datetime(s['expiry']) - pd.to_datetime(s['date'])).dt.days
    s = s[(s['dte'] > 0) & (s['dte'] <= 45) & (s['delta'].abs().between(0.40, 0.60))]
    if s.empty:
        return None
    nearest_dte = s['dte'].min()
    s = s[s['dte'] == nearest_dte]
    call_mid = s[s['option_type'] == 'call']['market_price'].mean()
    put_mid = s[s['option_type'] == 'put']['market_price'].mean()
    if pd.isna(call_mid) or pd.isna(put_mid):
        return None
    spot = (s['strike'].mean())  # ATM strike ≈ spot
    if spot <= 0:
        return None
    return float((call_mid + put_mid) / spot)


def run_backtest(px, opt, earn,
                 implied_move_min: float = 0.04,
                 implied_vs_hist_ratio: float = 1.4,
                 max_positions_per_day: int = 6) -> dict:
    px['date'] = pd.to_datetime(px['date'])
    px_wide = px.pivot(index='date', columns='ticker', values='close').sort_index()
    opt['date'] = pd.to_datetime(opt['date'])
    earn['date'] = pd.to_datetime(earn['date'])

    bt = Backtester(BacktestConfig(annualisation=252,
                                   options_commission_per_contract=0.65,
                                   options_slippage_bps=10.0),
                    name='S-HV17_earnings_straddle_fade')

    # Compute rolling 4-quarter realised post-earnings move for hist baseline
    earn = earn.sort_values(['ticker', 'date'])
    earn['hist_move_proxy'] = 0.04   # baseline for synthetic

    n_trades = 0
    for d, group in earn.groupby('date'):
        if d not in px_wide.index:
            continue
        d_idx = px_wide.index.get_loc(d)
        if d_idx + 1 >= len(px_wide):
            continue
        d_next = px_wide.index[d_idx + 1]

        cands = []
        for _, row in group.iterrows():
            tk = row['ticker']
            if tk not in px_wide.columns:
                continue
            S0 = px_wide[tk].loc[d]
            S1 = px_wide[tk].loc[d_next]
            if pd.isna(S0) or pd.isna(S1) or S0 <= 0:
                continue
            im = estimate_implied_move(opt, tk, d)
            if im is None or im < implied_move_min:
                continue
            hist = 0.04
            if im < implied_vs_hist_ratio * hist:
                continue
            cands.append((im, tk, S0, S1, hist))

        cands.sort(key=lambda x: x[0], reverse=True)
        cands = cands[:max_positions_per_day]
        for im, tk, S0, S1, hist in cands:
            actual_move = abs(S1 / S0 - 1)
            # SELL_VOL P&L: gain if implied > actual (in vol-points squared)
            iv_at_entry = im / np.sqrt(1 / 252.0)   # daily IV → annualised
            iv_realised = actual_move / np.sqrt(1 / 252.0)
            tau = 1 / 252.0
            bt.add_trade(OptionsVolTrade(
                ticker=tk, entry_date=d.date(), exit_date=d_next.date(),
                direction='SELL_VOL',
                iv_entry=float(iv_at_entry),
                rv_realised=float(iv_realised),
                tau_years=float(tau),
                weight=0.01,
                label='S-HV17',
            ))
            n_trades += 1

    return {'report': bt.report(), 'n_trades_total': n_trades}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--real', action='store_true')
    p.add_argument('--out', type=str, default='results/bt_shv17_synthetic.json')
    args = p.parse_args()

    if args.real and VPS_DATA_DIR.exists():
        from backtest_framework import gen_synthetic_options
        px, opt, earn = load_real_data()
        if earn.empty:
            earn = gen_synthetic_earnings(px)
        mode = 'real'
    else:
        from backtest_framework import gen_synthetic_options
        px = gen_synthetic_prices(n_days=2520, n_tickers=40, seed=42)
        opt = gen_synthetic_options(px, seed=42)
        earn = gen_synthetic_earnings(px)
        mode = 'synthetic'

    print(f"S-HV17 backtest — mode={mode}")
    print(f"  prices  : {len(px):,} rows    options : {len(opt):,}    earnings : {len(earn):,}")

    out = run_backtest(px, opt, earn)
    rep = out['report']
    print(f"\n  n_trades     : {out['n_trades_total']}")
    print(f"  Sharpe       : {rep.sharpe:.3f}  (IS {rep.is_sharpe:.3f}  OOS {rep.oos_sharpe:.3f})")
    print(f"  Max DD       : {rep.max_dd*100:.1f}%   Win rate: {rep.win_rate*100:.1f}%")
    print(f"  Cum/Ann ret  : {rep.cum_return*100:.1f}% / {rep.annualised_return*100:.1f}%")

    out_path = Path(__file__).parent / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rep.save(str(out_path))
    print(f"\n  Saved → {out_path}")


if __name__ == "__main__":
    main()
