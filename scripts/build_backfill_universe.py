#!/usr/bin/env python3
"""SP-2 Phase B: one-shot backfill-universe builder.

Output: data/.backfill_universe_v1.txt -- newline-delimited tickers.
Source: alpaca_tradable_universe filtered to active+tradable, ranked by
        90-day dollar volume (close * volume) computed from prices.parquet.

Operator usage:
    export $(grep -E "^POSTGRES_URI=" .env | head -1)
    python3 scripts/build_backfill_universe.py --top-n 3000

This is a frozen artifact; commit the output file once and never re-run as
a cron job. A future Phase B v2 would write a sibling file `.backfill_universe_v2.txt`.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2
import pyarrow.dataset as ds
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / '.backfill_universe_v1.txt'
MASTER_PRICES = ROOT / 'data' / 'master' / 'prices.parquet'

# Window for ranking (calendar days). 90 calendar days approximates 60 trading days.
ADV_WINDOW_DAYS = 90

# File-size guard: below this, just read the whole table (test fixtures, small
# parquets). Above, use the streaming dataset path with pushdown filter so we
# never load the multi-GB master prices.parquet into memory.
STREAMING_THRESHOLD_BYTES = 100 * 1024 * 1024  # 100 MB


def _load_active_tradable(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM alpaca_tradable_universe "
            "WHERE status='active' AND tradable=TRUE"
        )
        return {row[0] for row in cur.fetchall()}


def _rank_by_adv(prices_path: Path) -> pd.Series:
    """Return Series indexed by ticker with mean dollar-volume over the last
    ADV_WINDOW_DAYS calendar days, sorted desc.

    Streams the parquet via pyarrow.dataset with a pushdown filter on `date`
    when the file is large, so the multi-GB master prices.parquet never has
    to be materialised in memory in full. Small files (test fixtures) take
    the simpler read_table path.
    """
    if not prices_path.exists():
        raise FileNotFoundError(
            f"master prices parquet missing at {prices_path} -- "
            "cannot compute ADV ranking. Run the daily collector first."
        )

    size = prices_path.stat().st_size
    if size < STREAMING_THRESHOLD_BYTES:
        # Small file (test fixture or trivially-small parquet): read the whole
        # thing — keeps the simple code path exercised in tests.
        df = pq.read_table(
            str(prices_path), columns=['ticker', 'date', 'close', 'volume']
        ).to_pandas()
        if df.empty:
            raise ValueError(f"master prices parquet at {prices_path} is empty")
        # Dates are ISO 8601 strings; lexical sort matches chronological.
        max_date_str = df['date'].max()
        cutoff = (
            pd.to_datetime(max_date_str) - pd.Timedelta(days=ADV_WINDOW_DAYS)
        ).strftime('%Y-%m-%d')
        recent = df[df['date'] >= cutoff].copy()
    else:
        # Large file: stream. Step 1 — read just the `date` column to compute
        # max_date. Step 2 — read only the rows we need via dataset pushdown.
        dataset = ds.dataset(str(prices_path), format='parquet')
        date_only = dataset.to_table(columns=['date']).to_pandas()
        if date_only.empty:
            raise ValueError(f"master prices parquet at {prices_path} is empty")
        max_date_str = date_only['date'].max()
        cutoff = (
            pd.to_datetime(max_date_str) - pd.Timedelta(days=ADV_WINDOW_DAYS)
        ).strftime('%Y-%m-%d')
        recent_table = dataset.to_table(
            columns=['ticker', 'date', 'close', 'volume'],
            filter=ds.field('date') >= cutoff,
        )
        recent = recent_table.to_pandas()

    if recent.empty:
        raise ValueError(
            f"no rows within last {ADV_WINDOW_DAYS} days of {max_date_str} "
            f"in {prices_path}; cannot rank"
        )
    # NOTE: master prices.parquet has no `adj_close`; use `close * volume`.
    # ADV ranking is monotonic; using unadjusted close vs adjusted close
    # changes absolute dollar volume but rarely the top-N membership.
    # Drop rows with NaN close or volume: a NaN bar would silently lower the
    # mean below a ticker's true ADV (NaN -> dropped from groupby.mean).
    recent = recent.dropna(subset=['close', 'volume'])
    if recent.empty:
        raise ValueError(
            f"all rows within last {ADV_WINDOW_DAYS} days of {max_date_str} "
            f"in {prices_path} had NaN close or volume; cannot rank"
        )
    # Cast to float64 explicitly so 100M shares * $1000 close (1e11) doesn't
    # overflow if either column came back as int32 / float32 from the parquet.
    recent['dollar_vol'] = (
        recent['close'].astype('float64') * recent['volume'].astype('float64')
    )
    return recent.groupby('ticker')['dollar_vol'].mean().sort_values(ascending=False)


def main(top_n: int, force: bool = False) -> int:
    pg_uri = os.environ.get('POSTGRES_URI')
    if not pg_uri:
        sys.stderr.write(
            "POSTGRES_URI env var not set. Run: export $(grep -E '^POSTGRES_URI=' .env | head -1)\n"
        )
        return 2

    if OUT.exists() and not force:
        try:
            display = OUT.relative_to(ROOT)
        except ValueError:
            display = OUT
        sys.stderr.write(
            f"REFUSED: {display} already exists; pass --force to overwrite "
            "(and commit a v2.txt file instead, per the frozen-artifact convention)\n"
        )
        return 2

    conn = psycopg2.connect(pg_uri)
    try:
        active = _load_active_tradable(conn)
    finally:
        conn.close()
    if not active:
        sys.stderr.write(
            "alpaca_tradable_universe returned 0 active+tradable rows; "
            "refusing to write an empty backfill universe.\n"
        )
        return 1

    adv = _rank_by_adv(MASTER_PRICES)
    ranked = [t for t in adv.index if t in active][:top_n]
    if not ranked:
        sys.stderr.write(
            "Intersection of ADV-ranked prices.parquet tickers with "
            "active+tradable alpaca_tradable_universe is empty; refusing to write.\n"
        )
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(ranked) + '\n')
    print(f'Wrote {len(ranked)} tickers to {OUT}')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--top-n', type=int, default=3000,
                    help='Number of tickers to write (default 3000)')
    ap.add_argument('--force', action='store_true', default=False,
                    help='Overwrite the existing output file (frozen-artifact '
                         'convention says you should ship a v2.txt sibling instead)')
    args = ap.parse_args()
    sys.exit(main(args.top_n, force=args.force))
