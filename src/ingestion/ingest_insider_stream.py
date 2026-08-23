"""Daily global Form-4 stream → data/master/insider.parquet.

D4 (2026-08-23). The EOD collector walked `/stable/insider-trading/search?
symbol=X` for ALL ~11.8k active tickers every day — "Insider: 252,36x new
transactions | 0 tickers skipped (fresh)" in every daily log — because its
7-day freshness check compared data_coverage DATE RANGES, and yesterday's
`date_to` can never satisfy `requested_to = today`. That walk was the real
source of FMP rate pressure (11.8k calls/cycle for ~1.5–3k net-new rows).

`/stable/insider-trading/latest` is the global filing stream most-recent-first
at 1,000 rows/page; one page spans ~1.5 days (measured 2026-07-30), so a handful
of pages covers everything since the last run for EVERY symbol at once. The
14:30 ET overlay (`intraday_insider.py`) already reads it into a day-scoped
file; this module folds the same stream into the MASTER so the per-symbol walk
can drop to a weekly reconciliation.

Append-only: rows are inserted with `parquet_store.append_insert_only` on the
master's own key (ticker, filing_date, insider_name, transaction_type, shares);
nothing is ever dropped, tickers outside today's universe are kept (tickers may
be ADDED at any time — CLAUDE.md core invariant).

Counters are the contract ("read the COUNTERS, not the exit code"):
  rc=1  the stream could not be read at all (page 0 failed / no key)
  rc=0  otherwise, with fetched / new_rows / dup_rows / tickers printed.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date as _date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # invoked as `python3 src/ingestion/…` by the collector
    sys.path.insert(0, str(ROOT))

from src.ingestion.intraday_insider import (  # noqa: E402,F401 — re-exported for monkeypatching
    IntradayInsiderError, RAW_COLS, fetch_latest_filings, _master_max_date,
)

logger = logging.getLogger(__name__)
MASTER_PATH = ROOT / 'data' / 'master' / 'insider.parquet'

# The master's own dedup key (src/data/parquet_store.py INSIDER_KEYS).
MASTER_KEYS = ['ticker', 'filing_date', 'insider_name', 'transaction_type', 'shares']


def _since_for(master_max_date: str | None, *, overlap_days: int = 2,
               today: str | None = None, fallback_days: int = 5) -> str:
    """Start of the stream window. Overlaps the master's newest filing date by
    `overlap_days` (FMP paging repeats rows across boundaries and late rows
    can post after the day's first pass); dedup makes the overlap free."""
    if master_max_date:
        d = _date.fromisoformat(str(master_max_date)[:10]) - timedelta(days=overlap_days)
    else:
        base = _date.fromisoformat(today) if today else _date.today()
        d = base - timedelta(days=fallback_days)
    return d.isoformat()


def merge_stream_into_master(rows: list[dict], *, master_path: Path = MASTER_PATH) -> dict:
    """Insert-only merge of stream rows into the master. Returns counters."""
    from src.data.parquet_store import append_insert_only, row_count

    stats = {'fetched': len(rows), 'new_rows': 0, 'dup_rows': 0, 'dropped_invalid': 0,
             'tickers': 0, 'master_rows_after': None}
    df = pd.DataFrame(rows, columns=RAW_COLS) if rows else pd.DataFrame(columns=RAW_COLS)
    if df.empty:
        stats['master_rows_after'] = row_count(master_path) if Path(master_path).exists() else 0
        return stats

    valid = df['ticker'].astype(str).str.strip().ne('') & df['filing_date'].notna()
    stats['dropped_invalid'] = int((~valid).sum())
    df = df[valid].copy()
    df['ticker'] = df['ticker'].astype(str).str.strip().str.upper()
    df = df.drop_duplicates(subset=MASTER_KEYS)

    before = row_count(master_path) if Path(master_path).exists() else 0
    after = append_insert_only(master_path, df, MASTER_KEYS)
    stats['new_rows'] = int(after - before)
    stats['dup_rows'] = int(len(df) - stats['new_rows'])
    stats['tickers'] = int(df['ticker'].nunique())
    stats['master_rows_after'] = int(after)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--since', help='YYYY-MM-DD; default = master max filing date − 2 days')
    ap.add_argument('--max-pages', type=int, default=None)
    ap.add_argument('--budget', type=float, default=None, help='seconds')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    since = args.since or _since_for(_master_max_date())
    try:
        rows, fstats = fetch_latest_filings(since, budget_s=args.budget, max_pages=args.max_pages)
    except IntradayInsiderError as exc:
        print(f'[insider-stream] FAILED since={since}: {exc}', file=sys.stderr)
        return 1
    stats = merge_stream_into_master(rows, master_path=MASTER_PATH)
    print(f"[insider-stream] since={since} pages={fstats.get('pages')} raw={fstats.get('raw_rows')} "
          f"fetched={stats['fetched']} new_rows={stats['new_rows']} dup_rows={stats['dup_rows']} "
          f"dropped_invalid={stats['dropped_invalid']} tickers={stats['tickers']} "
          f"master_rows_after={stats['master_rows_after']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
