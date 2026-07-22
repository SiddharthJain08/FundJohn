"""
ingest_vol_indices.py
=====================
Daily ingest of vol-index closes (VIX, VVIX, VIX9D) to
/root/openclaw/data/master/vol_indices.parquet.

Strategies that depend on this file
----------------------------------
* S-TR-01 VVIX Early Warning     → vvix_close series + 252-day percentile
* S-TR-04 Zarattini Intraday SPY → vix_close (gate LONG when VIX < 30)
* S-HV15 IV Term Structure       → vix_close (sanity gate on inversion)
* S-HV20 IV Dispersion           → vix_close for regime scaling (optional)

Schema (long-format, one row per date):
    date (date)            — session date
    vix_close (float)      — CBOE VIX
    vvix_close (float)     — CBOE VVIX (vol-of-vol on VIX)
    vix9d_close (float)    — CBOE VIX9D (9-day variant)

Data source
-----------
Yahoo Finance (yfinance) — the FMP `historical-price-full` and Polygon
`I:VIX` index endpoints both return 403/NOT_AUTHORIZED on the current
plan tier. yfinance returns daily OHLCV for ^VIX/^VVIX/^VIX9D without
auth and is what backfill_vix.py + fetch_vol_indices.py use elsewhere.

Usage
-----
    python ingest_vol_indices.py                   # incremental append
    python ingest_vol_indices.py --rebuild --from 2016-01-01   # full rebuild
    python ingest_vol_indices.py --dry-run         # validate without writing

Author: Claude / FundJohn research, 2026-04-23.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path("/root/openclaw")
DATA_DIR = ROOT / "data" / "master"
OUT_PATH = DATA_DIR / "vol_indices.parquet"

INDEX_TICKERS = {
    'vix_close':   '^VIX',
    'vvix_close':  '^VVIX',
    'vix9d_close': '^VIX9D',
}

# Use cboe_vol_indices as the sole yfinance gateway for vol indices
sys.path.insert(0, str(ROOT))
from src.ingestion.cboe_vol_indices import _yf_download  # noqa: E402


def yfinance_historical(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Pull daily Close for a symbol via cboe_vol_indices._yf_download.
    `end` is INCLUSIVE (bumped +1 day at the yfinance boundary, whose `end`
    is exclusive — the 17:30 ET timer run was silently dropping the current
    day's close every run, leaving the parquet one trading day stale and
    tripping the 4-day doctor FAIL after each weekend; cf.
    cboe_vol_indices._fetch_series_for_macro which already does this).
    Returns DataFrame with columns ['date', 'close']. Empty DataFrame on failure."""
    end_excl = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    try:
        # auto_adjust=False keeps Close as the raw closing value (matches
        # what FMP and the existing macro.parquet VIX rows record).
        df = _yf_download(symbol, period=None, start=start, end=end_excl)
    except Exception as e:
        print(f"  yfinance download failed for {symbol}: {e}", file=sys.stderr)
        return pd.DataFrame(columns=['date', 'close'])
    if df is None or df.empty:
        return pd.DataFrame(columns=['date', 'close'])
    # yfinance returns a MultiIndex (field, ticker) when one symbol passed —
    # flatten by selecting Close column for the symbol.
    if isinstance(df.columns, pd.MultiIndex):
        if ('Close', symbol) in df.columns:
            close = df[('Close', symbol)]
        else:
            close = df.xs('Close', axis=1, level=0).iloc[:, 0]
    else:
        close = df['Close']
    out = close.reset_index()
    out.columns = ['date', 'close']
    out['date'] = pd.to_datetime(out['date']).dt.date
    return out.dropna(subset=['close']).reset_index(drop=True)


def load_existing() -> pd.DataFrame:
    if OUT_PATH.exists():
        return pd.read_parquet(OUT_PATH)
    return pd.DataFrame(columns=['date'] + list(INDEX_TICKERS.keys()))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--rebuild', action='store_true',
                   help='Rebuild full history instead of incremental append')
    p.add_argument('--from', dest='from_date', default='2016-01-01')
    p.add_argument('--to', dest='to_date', default=None)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = pd.DataFrame() if args.rebuild else load_existing()
    from_d = args.from_date
    to_d = args.to_date or date.today().isoformat()

    if not args.rebuild and not existing.empty:
        last = pd.to_datetime(existing['date']).max().date()
        from_d = (last - timedelta(days=3)).isoformat()  # small overlap for safety

    print(f"vol_indices ingest — range {from_d} → {to_d}")

    frames = {}
    for col, sym in INDEX_TICKERS.items():
        print(f"  pulling {sym:8s} → {col}")
        df = yfinance_historical(sym, from_d, to_d)
        frames[col] = df.rename(columns={'close': col}).set_index('date')

    out = pd.concat(frames.values(), axis=1, join='outer').reset_index()
    out = out.sort_values('date').reset_index(drop=True)
    # Forward-fill 1 day to bridge isolated index holidays
    for col in INDEX_TICKERS:
        if col in out.columns:
            out[col] = out[col].ffill(limit=1)
    out = out.dropna(subset=list(INDEX_TICKERS.keys()), how='all')

    # Merge with existing
    if not existing.empty:
        combined = pd.concat([existing, out], ignore_index=True)
        combined = combined.drop_duplicates(subset=['date'], keep='last')
        combined = combined.sort_values('date').reset_index(drop=True)
    else:
        combined = out

    print(f"  rows new        : {len(out):,}")
    print(f"  rows total      : {len(combined):,}")
    print(f"  last date       : {combined['date'].max()}")

    if args.dry_run:
        print("  --dry-run: not writing")
        return

    combined.to_parquet(OUT_PATH, index=False)
    print(f"  wrote → {OUT_PATH}")


if __name__ == "__main__":
    main()
