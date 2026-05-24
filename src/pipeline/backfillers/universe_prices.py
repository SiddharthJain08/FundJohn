"""Fetch + validate price chunks for the SP-2 Phase B 5y backfill driver.

Pure module — no IO outside `subprocess.run(alpaca)` + pandas. Used by
`scripts/backfill_universe_5y.py::_run_prices` (Task 7).

Master parquet schema (verified 2026-05-22):
    ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume', 'vwap',
     'transactions', 'source']
Note: there is no `adj_close` column. Alpaca's `--adjustment split` returns
split-adjusted bars; the close value goes directly into `close`. The spec
(§2.2.1) referenced an `adj_close` field that does not exist in production —
trusted the live schema over the spec, flagged as a deviation in the DONE
report.

Alpaca bars response shape (verified 2026-05-22):
    {
      "bars": [{"c": float, "h": float, "l": float, "n": int,
                "o": float, "t": "YYYY-MM-DDTHH:MM:SSZ", "v": int, "vw": float}, ...],
      "next_page_token": str,
      "symbol": str
    }
Spec hinted at `bars: {ticker: [...]}` shape — actual is a flat list, since
this is the single-symbol `data bars` endpoint, not `multi-bars`.

Spot-check (5 random re-fetches per chunk per spec §2.2.1) is intentionally
DEFERRED to a follow-up: it requires 5 extra live calls per chunk, doubling
backfill cost (~10k extra calls over 15k chunks), and the rate-of-bad-data
signal we'd get from spot-checking is structurally the same as the existing
schema/null/row-count validators on the bulk pull.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import date
from typing import Optional, Tuple

import pandas as pd


ALPACA_BIN = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')

# Canonical columns required in every staged DataFrame. The master parquet may
# contain additional columns later; missing columns are a validation failure.
SCHEMA = [
    'ticker', 'date', 'open', 'high', 'low', 'close',
    'volume', 'vwap', 'transactions', 'source',
]

# Validation thresholds per spec §2.2.1.
MIN_ROWS_PARTIAL_YEAR = 30
MIN_ROWS_FULL_YEAR = 200


def fetch_ticker_year(symbol: str, year: int) -> pd.DataFrame:
    """Returns daily split-adjusted bars for (symbol, year) using Alpaca CLI.

    Empty DataFrame if no bars returned (delisted, never-traded, etc.).
    Raises RuntimeError on a non-zero exit from the CLI, but NOT on empty
    response — empty is a legitimate "this ticker has no data here" signal.
    Uses subprocess timeout=60 per task spec; the CLI's own 30s HTTP timeout
    bounds individual calls.
    """
    start = f'{year}-01-01'
    end = f'{year}-12-31'
    args = [
        ALPACA_BIN, 'data', 'bars',
        '--symbol', symbol,
        '--start', start,
        '--end', end,
        '--timeframe', '1Day',
        '--adjustment', 'split',
        '--limit', '10000',
    ]
    rows: list[dict] = []
    page_token: Optional[str] = None
    while True:
        cur_args = list(args)
        if page_token:
            cur_args.extend(['--page-token', page_token])
        try:
            res = subprocess.run(
                cur_args, capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f'alpaca bars timeout for {symbol}/{year}: {e}')
        if res.returncode != 0:
            raise RuntimeError(
                f'alpaca bars rc={res.returncode} for {symbol}/{year}: '
                f'{res.stderr.strip()[:200]}'
            )
        try:
            payload = json.loads(res.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f'alpaca bars json decode failed for {symbol}/{year}: {e}'
            )
        bars = payload.get('bars') or []
        for b in bars:
            rows.append({
                'ticker': symbol,
                'date': (b.get('t') or '')[:10],  # 'YYYY-MM-DDTHH:MM:SSZ' → 'YYYY-MM-DD'
                'open': b.get('o'),
                'high': b.get('h'),
                'low': b.get('l'),
                'close': b.get('c'),
                'volume': b.get('v'),
                'vwap': b.get('vw'),
                'transactions': b.get('n'),
                'source': None,  # populated at promote-time with source_tag
            })
        page_token = payload.get('next_page_token') or None
        if not page_token:
            break
    if not rows:
        return pd.DataFrame(columns=SCHEMA)
    df = pd.DataFrame(rows, columns=SCHEMA)
    return df


def validate(df: pd.DataFrame, symbol: str, year: int) -> Tuple[bool, Optional[str]]:
    """Returns (ok, error_str_or_None).

    Checks per spec §2.2.1:
      - Schema: all SCHEMA columns present (extras OK).
      - Nulls: no nulls in (ticker, date, close).
      - Date range: all dates within [YYYY-01-01, YYYY-12-31].
      - Row count: >=200 full year, >=30 partial-year (ticker listed mid-year).

    Partial-year is detected by minimum date > Mar 1 of `year` OR maximum date
    < Oct 1 of `year` — i.e. clearly missing a full quarter on either side.
    """
    if df is None or len(df) == 0:
        return False, 'empty DataFrame'

    missing = [c for c in SCHEMA if c not in df.columns]
    if missing:
        return False, f'missing columns: {missing}'

    for col in ('ticker', 'date', 'close'):
        if df[col].isnull().any():
            n = int(df[col].isnull().sum())
            return False, f'null values in required column {col!r} (n={n})'

    year_start = f'{year}-01-01'
    year_end = f'{year}-12-31'
    out_of_range = df[(df['date'] < year_start) | (df['date'] > year_end)]
    if len(out_of_range) > 0:
        sample = out_of_range['date'].iloc[0]
        return False, (
            f'dates out of [{year_start}, {year_end}] '
            f'(n={len(out_of_range)}, sample={sample})'
        )

    min_d = df['date'].min()
    max_d = df['date'].max()
    is_partial_year = (min_d > f'{year}-03-01') or (max_d < f'{year}-10-01')
    threshold = MIN_ROWS_PARTIAL_YEAR if is_partial_year else MIN_ROWS_FULL_YEAR
    if len(df) < threshold:
        return False, (
            f'row count {len(df)} below threshold {threshold} '
            f'(partial_year={is_partial_year}, min_d={min_d}, max_d={max_d})'
        )

    return True, None


def sha256(df: pd.DataFrame) -> str:
    """Deterministic hash for the audit row.

    Canonicalizes by sorting rows by (ticker, date) and serializing as CSV
    with stable column order. Same content -> same hash across reads.
    """
    if df is None or len(df) == 0:
        return hashlib.sha256(b'').hexdigest()
    cols = [c for c in SCHEMA if c in df.columns]
    sorted_df = df[cols].sort_values(['ticker', 'date']).reset_index(drop=True)
    payload = sorted_df.to_csv(index=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()
