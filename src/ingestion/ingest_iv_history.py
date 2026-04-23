"""
ingest_iv_history.py
====================
Derive a per-ticker ATM-30d IV history series from existing
`options_eod.parquet` snapshots, writing to
/root/openclaw/data/master/iv_history.parquet.

Strategies that depend on this file
----------------------------------
* S-HV13 Call-Put IV Spread   → iv_rank (percentile of current IV in its
                                 trailing 252-day window — confidence scaling)
* S-HV14 OTM Skew Factor      → iv_rank (same role)
* S-HV15 IV Term Structure    → iv_rank (confidence scaling on inversion)
* S-HV17 Earnings Straddle    → atm_iv_30d historical series (for implied
                                 vs. realised-move ratio baseline)

Schema:
    ticker (str)
    date (date)
    iv_30d (float)    — ATM call-IV for nearest-to-30-DTE expiry
    iv_90d (float)    — same, 90-DTE
    ts_ratio (float)  — iv_30d / iv_90d

Usage
-----
    python ingest_iv_history.py                      # incremental append
    python ingest_iv_history.py --rebuild            # full rebuild from 2020-01-01
    python ingest_iv_history.py --dry-run

Author: Claude / FundJohn research, 2026-04-23.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

DATA_DIR = Path("/root/openclaw/data/master")
OPTS_EOD = DATA_DIR / "options_eod.parquet"
OUT_PATH = DATA_DIR / "iv_history.parquet"


def _atm_iv_for_dte(chain: pd.DataFrame, target_dte: int,
                    tol: int = 7) -> Optional[float]:
    """ATM call-IV (|Δ|∈[0.45,0.55]) for the listed expiry nearest target_dte."""
    if chain.empty:
        return None
    today = pd.to_datetime(chain['date'].iloc[0])
    dte = (pd.to_datetime(chain['expiry']) - today).dt.days
    sel = chain[dte.between(target_dte - tol, target_dte + tol)].copy()
    if sel.empty:
        return None
    sel['dte'] = (pd.to_datetime(sel['expiry']) - today).dt.days
    sel['dte_dist'] = (sel['dte'] - target_dte).abs()
    sel = sel[sel['dte_dist'] == sel['dte_dist'].min()]
    atm = sel[(sel['option_type'] == 'call') &
              (sel['delta'].between(0.45, 0.55))]
    if atm.empty:
        return None
    return float(atm['implied_volatility'].mean())


def collapse_day(chain: pd.DataFrame) -> Optional[dict]:
    """For one ticker-date snapshot, return {iv_30d, iv_90d, ts_ratio}."""
    iv30 = _atm_iv_for_dte(chain, 30)
    iv90 = _atm_iv_for_dte(chain, 90)
    if iv30 is None and iv90 is None:
        return None
    ts = (iv30 / iv90) if (iv30 and iv90 and iv90 > 0) else None
    return {'iv_30d': iv30, 'iv_90d': iv90, 'ts_ratio': ts}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--rebuild', action='store_true')
    p.add_argument('--since', default=None,
                   help='Process ticker-dates on/after this date (YYYY-MM-DD)')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    if not OPTS_EOD.exists():
        print(f"ERROR: {OPTS_EOD} not found — run ingest_options first.",
              file=sys.stderr)
        sys.exit(2)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = pd.DataFrame()
    since_d = None
    if not args.rebuild and OUT_PATH.exists():
        existing = pd.read_parquet(OUT_PATH)
        if not existing.empty:
            since_d = pd.to_datetime(existing['date']).max()

    if args.since:
        since_d = pd.to_datetime(args.since)

    print(f"iv_history ingest — rebuild={args.rebuild}  "
          f"since={since_d}  existing_rows={len(existing):,}")

    # Stream through options_eod by ticker-date partition to limit memory
    opts = pd.read_parquet(OPTS_EOD, columns=[
        'ticker', 'date', 'expiry', 'strike', 'option_type',
        'delta', 'implied_volatility',
    ])
    opts['date'] = pd.to_datetime(opts['date'])
    if since_d is not None:
        opts = opts[opts['date'] > since_d]
    print(f"  filtering → {len(opts):,} option rows in window")

    records = []
    for (tk, d), chain in opts.groupby(['ticker', 'date']):
        row = collapse_day(chain)
        if row is None:
            continue
        records.append({'ticker': tk, 'date': d.date(), **row})

    new_df = pd.DataFrame(records)
    print(f"  new (ticker, date) rows: {len(new_df):,}")

    if not existing.empty and not new_df.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['ticker', 'date'],
                                            keep='last')
    elif new_df.empty:
        combined = existing
    else:
        combined = new_df
    combined = combined.sort_values(['ticker', 'date']).reset_index(drop=True)

    print(f"  total rows        : {len(combined):,}")
    print(f"  tickers covered   : {combined['ticker'].nunique()}")

    if args.dry_run:
        print("  --dry-run: not writing")
        return

    combined.to_parquet(OUT_PATH, index=False)
    print(f"  wrote → {OUT_PATH}")


if __name__ == "__main__":
    main()
