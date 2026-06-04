"""Self-sustaining daily refresh for prices_30m.parquet.

Universe = every distinct ticker already present in the master parquet
(self-sustaining: refresh what exists; never-delete guard means tickers only
ever accumulate).  Per-run window: start = (max datetime across the whole
parquet, converted to ET calendar date) minus 2 calendar days (overlap is
safe — the ingester dedups on (ticker, datetime)); end = today (ET).

The backfill() / DATA_PARQUET names are imported from the lifted Alpaca
ingester (ingest_prices_30m_alpaca.py).  We do NOT duplicate any fetch /
append / atomic-write logic here.

Usage (from repo root):
  python3 src/ingestion/ingest_prices_30m_daily.py [--from YYYY-MM-DD] [--dry-run]

Or as a module under pytest (PYTHONPATH=src):
  from ingestion.ingest_prices_30m_daily import run
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from typing import List, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# Import backfill + DATA_PARQUET from the sibling Alpaca ingester.
#
# pytest  (PYTHONPATH=src):           `from ingestion.X` works directly.
# script  (python3 src/ingestion/Y.py from repo root):
#   sys.path has only the script's own dir (src/ingestion/).  Neither
#   `ingestion.X` nor `src.ingestion.X` resolves from there — we must do a
#   bare sibling import after inserting the file's own directory on sys.path.
# ---------------------------------------------------------------------------
try:
    from ingestion.ingest_prices_30m_alpaca import backfill, DATA_PARQUET
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ingest_prices_30m_alpaca import backfill, DATA_PARQUET  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Pure helper: derive universe from the parquet DataFrame
# ---------------------------------------------------------------------------

def derive_universe(df: pd.DataFrame) -> List[str]:
    """Return a sorted list of distinct tickers from the DataFrame.

    We key on the 'ticker' column only — the `datetime` axis is the reliable
    axis in prices_30m.parquet (some legacy rows have NaN `date` strings) and
    we must NOT drop tickers just because their `date` string is null.
    """
    return sorted(df['ticker'].dropna().unique().tolist())


# ---------------------------------------------------------------------------
# Pure helper: compute the refresh window
# ---------------------------------------------------------------------------

def compute_window(df: pd.DataFrame, from_override: Optional[str] = None) -> Tuple[str, str]:
    """Return (start_str, end_str) as 'YYYY-MM-DD'.

    start: max(datetime) → ET calendar date − 2 calendar days
           (or from_override if given)
    end:   today in ET.
    """
    import pytz
    et_tz = pytz.timezone('America/New_York')
    today_et = date.today()  # server may be UTC; caller is responsible for TZ awareness

    if from_override:
        start_str = from_override
    else:
        max_dt = df['datetime'].max()
        # Convert tz-aware UTC timestamp to ET calendar date
        max_et_date = max_dt.tz_convert(et_tz).date()
        start_date = max_et_date - timedelta(days=2)
        start_str = start_date.isoformat()

    end_str = today_et.isoformat()
    return start_str, end_str


# ---------------------------------------------------------------------------
# Main entry point (callable from tests via run(argv=[...]) or from CLI)
# ---------------------------------------------------------------------------

def run(argv: Optional[List[str]] = None) -> int:
    """Driver entry point.  Returns an exit code (0 = success, 1 = error).

    Parameters
    ----------
    argv : list[str] or None
        Argument list to parse (default: sys.argv[1:]).
    """
    ap = argparse.ArgumentParser(description='Self-sustaining daily refresh of prices_30m.parquet')
    ap.add_argument('--from', dest='from_date', metavar='YYYY-MM-DD',
                    help='Override window start (default: max datetime - 2d)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Print universe + window; do NOT fetch or write')
    a = ap.parse_args(argv)

    # Guard: parquet MUST already exist and be non-empty.
    # NEVER create a fresh parquet — that would bypass append-only lineage.
    if not os.path.exists(DATA_PARQUET):
        print(f'ERROR: master parquet not found: {DATA_PARQUET}', file=sys.stderr)
        print('Run the initial backfill first (ingest_prices_30m_alpaca.py --tickers ...)',
              file=sys.stderr)
        sys.exit(1)

    df = pd.read_parquet(DATA_PARQUET)
    if len(df) == 0:
        print(f'ERROR: master parquet is empty: {DATA_PARQUET}', file=sys.stderr)
        sys.exit(1)

    universe = derive_universe(df)
    if not universe:
        print('ERROR: no tickers found in master parquet', file=sys.stderr)
        sys.exit(1)

    start, end = compute_window(df, from_override=a.from_date)

    if a.dry_run:
        print(f'[dry-run] universe ({len(universe)} tickers): {", ".join(universe)}')
        print(f'[dry-run] window: {start}..{end}')
        print('[dry-run] no fetch, no write')
        return 0

    before, after = backfill(universe, start, end, out_path=DATA_PARQUET)
    added = after - before

    # Final summary line — collector tails last 2 stdout lines into Discord
    print(f'prices_30m daily: {len(universe)} tickers, window {start}..{end}, '
          f'+{added} rows (total {after})')
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == '__main__':
    main()
