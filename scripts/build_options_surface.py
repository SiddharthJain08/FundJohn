#!/usr/bin/env python3
# scripts/build_options_surface.py
"""Build data/master/options_surface.parquet — one row per (ticker, session)
from options_eod.parquet via strategies.options_surface (spec 2026-09-04 A.7).

Replaces scripts/build_options_aggregates.py as stage 1 of
refresh_options_aggregates.py. Filtered, chunked reads (5 sessions per pass);
spot from prices.parquet; rows upserted with append_dedup on (ticker, date).
The monthly options_aggregates/ files are left untouched and unread.

Usage:
  python3 scripts/build_options_surface.py --start 2026-06-29 --end 2026-09-03 [--tickers SPY,AAPL] [--path …]
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / 'src'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
from src.data.parquet_store import SURFACE_KEYS, SURFACE_PATH, append_dedup  # noqa: E402
from strategies.options_surface import (SCALAR_KEYS, OPTIONS_FEATURES_VERSION,  # noqa: E402
                                        features_for_day, prepare_chain)
from strategies.options_oi import OI_KEYS  # noqa: E402 — Part B (task 13); landed, no longer optional

OPTS_PATH = ROOT / 'data' / 'master' / 'options_eod.parquet'
PRICES_PATH = ROOT / 'data' / 'master' / 'prices.parquet'
COLS = ['ticker', 'date', 'expiry', 'strike', 'option_type', 'implied_volatility',
        'delta', 'gamma', 'theta', 'vega', 'volume']
CHUNK_DAYS = 5
OUT_COLS = ['ticker', 'date'] + SCALAR_KEYS + OI_KEYS + ['built_at']


def _read_range(start: pd.Timestamp, end: pd.Timestamp, tickers=None) -> pd.DataFrame:
    flt = (pc.field('date') >= pc.scalar(start.strftime('%Y-%m-%d'))) & \
          (pc.field('date') <= pc.scalar(end.strftime('%Y-%m-%d')))
    if tickers:
        flt = flt & pc.field('ticker').isin(list(tickers))
    tbl = pq.read_table(OPTS_PATH, columns=COLS, filters=flt, read_dictionary=['ticker', 'option_type'])
    df = tbl.to_pandas()
    del tbl
    if df.empty:
        return df
    df['ticker'] = df['ticker'].astype(str)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    return df.dropna(subset=['date', 'ticker'])


def read_spots(tickers, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    flt = (pc.field('date') >= pc.scalar(start.strftime('%Y-%m-%d'))) & \
          (pc.field('date') <= pc.scalar(end.strftime('%Y-%m-%d')))
    if tickers:
        flt = flt & pc.field('ticker').isin(list(tickers))
    px = pq.read_table(PRICES_PATH, columns=['ticker', 'date', 'close'], filters=flt,
                       read_dictionary=['ticker']).to_pandas()
    px['ticker'] = px['ticker'].astype(str)
    px['date'] = pd.to_datetime(px['date'])
    return {(r.ticker, r.date): float(r.close) for r in px.itertuples() if r.close == r.close}


def build_rows(chain: pd.DataFrame, spots: dict, oi_lookup=None) -> pd.DataFrame:
    """One surface row per (ticker, date). `oi_lookup(ticker, date) -> dict | None`
    (Part B) merges open-interest keys when supplied."""
    stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    chain = chain.assign(date=pd.to_datetime(chain['date']).dt.normalize(), ticker=chain['ticker'].astype(str))
    rows = []
    for (ticker, day), grp in chain.groupby(['ticker', 'date'], sort=True):
        prepared = prepare_chain(grp, day)
        row = features_for_day(prepared, spots.get((ticker, day)), day)
        if oi_lookup is not None:
            row.update(oi_lookup(ticker, day) or {})
        # OI_KEYS are always written — present (None when no lookup was supplied,
        # or the lookup found no CBOE session/rows) so the surface master carries
        # every OI column from now on, never only when a session happened to exist.
        rows.append({'ticker': ticker, 'date': day.date(), **{k: row.get(k) for k in SCALAR_KEYS},
                     **{k: row.get(k) for k in OI_KEYS},
                     'built_at': stamp})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=OUT_COLS)
    df['options_features_version'] = OPTIONS_FEATURES_VERSION
    # Keep OI_KEYS as object dtype so a genuinely-missing feature stays `None`
    # rather than being upcast to NaN alongside another row's real float value
    # in the same column (pandas' default when a column mixes None and float).
    for k in OI_KEYS:
        df[k] = pd.Series([r.get(k) for r in rows], index=df.index, dtype=object)
    return df


def run(start: str, end: str, tickers=None, path=None, oi_lookup=None) -> int:
    path = Path(path or SURFACE_PATH)
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    total = 0
    t0 = time.time()
    cur = s
    while cur <= e:
        ce = min(cur + pd.Timedelta(days=CHUNK_DAYS - 1), e)
        chain = _read_range(cur, ce, tickers)
        if not chain.empty:
            spots = read_spots(sorted(chain['ticker'].unique()), cur - pd.Timedelta(days=7), ce)
            rows = build_rows(chain, spots, oi_lookup)
            del chain
            if not rows.empty:
                total = append_dedup(path, rows, SURFACE_KEYS, mode='replace')
                print(f'[options_surface] {cur.date()}..{ce.date()} rows={len(rows):,} master={total:,} '
                      f'{time.time() - t0:.0f}s', flush=True)
        cur = ce + pd.Timedelta(days=1)
    print(f'[options_surface] done {start}..{end} master_rows={total:,}', flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--tickers', default=None)
    ap.add_argument('--path', default=None)
    a = ap.parse_args(argv)
    tickers = [t.strip() for t in a.tickers.split(',')] if a.tickers else None
    oi_lookup = None
    try:
        from strategies.options_oi import oi_lookup_factory          # Part B (task 13); absent until then
        oi_lookup = oi_lookup_factory()
    except ImportError:
        pass
    return run(a.start, a.end, tickers, a.path, oi_lookup)


if __name__ == '__main__':
    sys.exit(main())
