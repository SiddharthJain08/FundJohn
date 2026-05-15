"""End-to-end demonstration of the StrategyTemplate ABC.

Loads SPY's last ~3 years from data/master/prices.parquet, runs the
SMACrossoverExample reference strategy through the template's run() driver
with the alpaca_fee commission model, and prints a stats summary the
operator can eyeball.

Saves the equity curve to output/template_demo_equity.csv for visual
inspection (pandas / Excel / quick gnuplot).

Usage:
    python3 scripts/demo_strategy_template.py [--ticker SPY] [--years 3]
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd

from src.strategies.strategy_template_base import run
from src.strategies.implementations._template_example import SMACrossoverExample, alpaca_fee


def _load_prices(ticker: str, years: int) -> pd.DataFrame:
    """Returns OHLCV DataFrame indexed by date.

    prices.parquet schema (as of 2026-05-15):
      ticker (str), date (str YYYY-MM-DD), open, high, low, close, volume,
      vwap, transactions, source.
    """
    parquet_path = os.path.join(ROOT, 'data', 'master', 'prices.parquet')
    df = pd.read_parquet(parquet_path)
    if 'ticker' in df.columns:
        df = df[df['ticker'] == ticker].copy()
    if df.empty:
        raise SystemExit(f"No prices for {ticker} in {parquet_path}")

    # Ensure DatetimeIndex
    date_col = next((c for c in ('ts', 'date', 'd') if c in df.columns), None)
    if date_col:
        df = df.set_index(pd.to_datetime(df[date_col]))
    df = df.sort_index()

    cutoff = df.index.max() - pd.DateOffset(years=years)
    df = df.loc[df.index >= cutoff]

    # Normalize column names — case insensitive
    df = df.rename(columns={c: c.lower() for c in df.columns})
    needed = ['open', 'high', 'low', 'close', 'volume']
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise SystemExit(f"prices.parquet missing columns {missing}; got {list(df.columns)}")
    return df[needed]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--ticker', default='SPY')
    ap.add_argument('--years',  type=int, default=3)
    args = ap.parse_args()

    print(f"Loading {args.ticker} prices, last {args.years}y...", file=sys.stderr)
    prices = _load_prices(args.ticker, args.years)
    print(f"  {len(prices)} bars from {prices.index.min().date()} to {prices.index.max().date()}",
          file=sys.stderr)

    print("Running SMACrossoverExample with alpaca_fee...", file=sys.stderr)
    result = run(SMACrossoverExample, prices, commission=alpaca_fee, close_at_eod=True)

    # Stats summary
    print()
    print(f"=== {args.ticker} {args.years}y SMACrossoverExample backtest ===")
    print(f"  Bars                  : {len(prices):,}")
    print(f"  Fills                 : {result['fills']}")
    print(f"  Round-trip trades     : {result['n_trades']}")
    print(f"  Total return          : {result['total_return_pct']:+.2f}%")
    print(f"  Sharpe (annualized)   : {result['sharpe_ratio']:.2f}")
    print(f"  Max drawdown          : {result['max_drawdown_pct']:.2f}%")
    print(f"  Win rate              : {result['win_rate_pct']:.1f}%")
    print(f"  Avg trade PnL         : ${result['avg_trade_pnl_usd']:+,.2f}")
    print(f"  Total commissions     : ${result['total_commission']:+,.2f}")
    print(f"  Position at end       : {result['position_at_end']}")

    # Save equity curve
    out_path = os.path.join(ROOT, 'output', 'template_demo_equity.csv')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    result['equity_curve'].rename('equity').to_csv(out_path, header=True)
    print(f"\nEquity curve saved to {out_path}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
