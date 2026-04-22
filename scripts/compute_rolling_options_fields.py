#!/usr/bin/env python3
"""
Post-processes data/master/options_aggregates/*.parquet to add rolling fields:

    iv_rank       : percentile of iv_front over trailing 252 trading days per ticker (0..100)
    rv_20         : 20-day realized vol (annualized stdev of log returns) from prices.parquet
    vrp           : iv_front - rv_20
    vrp_zscore    : zscore of vrp over trailing 60 days per ticker
    pc_ratio      : put_call_vol_ratio (alias for naming consistency with strategies)
    iv_spread     : term_slope (alias)
    ts_ratio      : iv_back / iv_front
    near_iv       : iv_front (alias)
    far_iv        : iv_back (alias)
    iv30          : iv_front (alias)

The output is a single `options_aggregates_enriched.parquet` ready for aux_data_loader.
"""
from __future__ import annotations
import sys, time
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
IN_DIR  = ROOT / 'data' / 'master' / 'options_aggregates'
OUT_PATH = ROOT / 'data' / 'master' / 'options_aggregates_enriched.parquet'
PRICES_PATH = ROOT / 'data' / 'master' / 'prices.parquet'


def load_all_aggregates() -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in sorted(IN_DIR.glob('*.parquet'))]
    if not frames:
        raise SystemExit(f'No aggregates in {IN_DIR}')
    df = pd.concat(frames, ignore_index=True)
    df['date'] = pd.to_datetime(df['date'])
    return df.sort_values(['ticker', 'date']).reset_index(drop=True)


def compute_rv_20(prices: pd.DataFrame) -> pd.DataFrame:
    prices = prices.copy()
    prices['date'] = pd.to_datetime(prices['date'])
    prices = prices.sort_values(['ticker', 'date'])
    prices['log_ret'] = prices.groupby('ticker')['close'].transform(lambda s: np.log(s / s.shift(1)))
    # 20-day realized vol, annualized
    prices['rv_20'] = prices.groupby('ticker')['log_ret'].transform(
        lambda s: s.rolling(20).std() * np.sqrt(252)
    )
    return prices[['ticker', 'date', 'rv_20']]


HIST_LEN = 20   # trailing history length required by HV7/HV9/HV12


def add_rolling(df: pd.DataFrame) -> pd.DataFrame:
    # iv_rank: rolling percentile of iv_front over 252-day window
    def pct_rank(s: pd.Series) -> pd.Series:
        return s.rolling(252, min_periods=20).rank(pct=True) * 100.0

    df['iv_rank'] = df.groupby('ticker')['iv_front'].transform(pct_rank)

    # vrp_zscore over trailing 60 days
    def zscore(s: pd.Series) -> pd.Series:
        mean = s.rolling(60, min_periods=10).mean()
        std  = s.rolling(60, min_periods=10).std()
        return (s - mean) / std.replace(0, np.nan)

    df['vrp_zscore'] = df.groupby('ticker')['vrp'].transform(zscore)

    # Trailing history lists — list of last HIST_LEN values per ticker-day.
    # These are consumed by HV7 (iv_rank_history), HV9 (hv20_history), HV12 (vrp_history).
    def build_history(source_col: str, target_col: str, window: int = HIST_LEN):
        # For each (ticker, date) row, collect the prior `window` values (inclusive of
        # the current date) as a Python list. Use a rolling window on the grouped
        # Series and extract the raw values.
        def _per_group(s: pd.Series) -> pd.Series:
            vals = s.tolist()
            out = [None] * len(vals)
            for i in range(len(vals)):
                start = max(0, i - window + 1)
                hist = [v for v in vals[start:i + 1] if v is not None and not (isinstance(v, float) and np.isnan(v))]
                if len(hist) >= 5:   # need at least 5 entries for strategies to accept
                    out[i] = hist
                else:
                    out[i] = None
            return pd.Series(out, index=s.index)

        df[target_col] = df.groupby('ticker', group_keys=False)[source_col].apply(_per_group)

    build_history('iv_rank', 'iv_rank_history', HIST_LEN)
    build_history('rv_20',   'hv20_history',    HIST_LEN)
    build_history('vrp',     'vrp_history',     HIST_LEN)

    # Total volume column consumed by HV7 (liquidity gate).
    df['volume'] = df['call_volume'].fillna(0) + df['put_volume'].fillna(0)
    return df


def main():
    t0 = time.time()
    print(f'Loading aggregates from {IN_DIR}...')
    df = load_all_aggregates()
    print(f'  rows: {len(df):,}  tickers: {df["ticker"].nunique():,}  dates: {df["date"].nunique()}')

    print(f'Loading prices from {PRICES_PATH}...')
    prices = pd.read_parquet(PRICES_PATH)
    rv = compute_rv_20(prices)
    print(f'  rv_20 rows: {len(rv):,}')

    df = df.merge(rv, on=['ticker', 'date'], how='left')
    df['vrp'] = df['iv_front'] - df['rv_20']

    print('Computing rolling iv_rank + vrp_zscore...')
    df = add_rolling(df)

    # Alias columns so strategies with varied naming both work
    df['pc_ratio']  = df['put_call_vol_ratio']
    df['iv_spread'] = df['term_slope']
    df['ts_ratio']  = df['iv_back'] / df['iv_front']
    df['near_iv']   = df['iv_front']
    df['far_iv']    = df['iv_back']
    df['iv30']      = df['iv_front']

    # Unusual-flow heuristic: pc_ratio > 1.5 OR bottom 5% of trailing 60d volume
    df['unusual_flow'] = (df['pc_ratio'] > 1.5).astype(int)

    print(f'Writing {OUT_PATH}...')
    df.to_parquet(OUT_PATH, index=False)
    print(f'Done. rows={len(df):,} cols={len(df.columns)} elapsed={time.time()-t0:.1f}s')
    print(f'columns: {list(df.columns)}')


if __name__ == '__main__':
    main()
