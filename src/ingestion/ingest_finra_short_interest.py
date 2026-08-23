"""FINRA biweekly consolidated short interest → data/master/short_interest.parquet.

Source (keyless, no special headers; verified 2026-08-23):
    GET https://cdn.finra.org/equity/otcmarket/biweekly/shrt{YYYYMMDD}.csv
pipe-delimited, one row per symbol (~22k rows / 2.1 MB per settlement date),
header:
    accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|
    marketClassCode|currentShortPositionQuantity|previousShortPositionQuantity|
    stockSplitFlag|averageDailyVolumeQuantity|daysToCoverQuantity|revisionFlag|
    changePercent|changePreviousNumber|settlementDate

Settlement dates are the 15th and the last business day of each month, rolled
BACK to the previous business day on weekends/holidays. FINRA publishes ~9
business days after settlement. The CDN answers 403 for any date that is NOT
a settlement date OR is not yet published — that is INFO ("unpublished"), never
an error. `candidate_settlement_dates` rolls over weekends only; a holiday
candidate 403s and the fetch loop then tries the 1–2 preceding business days.
The CSV's own `settlementDate` column is authoritative and is what gets stored,
whatever candidate URL fetched it.

Master: data/master/short_interest.parquet, key (settlement_date, ticker),
`parquet_store.append_dedup(mode='replace')` (FINRA revises — revisionFlag).
APPEND-ONLY: rows / tickers are only ever added (CLAUDE.md core invariant);
every symbol in the file is kept.

Counters are the contract ("read the COUNTERS, not the exit code"):
  rc=1  the provider could not be read at all: the first request raised, or
        EVERY candidate in the window failed with a non-403 error
  rc=0  otherwise, with files_fetched / files_skipped / files_unpublished /
        files_error / fetched / new_rows / replaced_rows printed.

Usage:
    python3 src/ingestion/ingest_finra_short_interest.py                 # last 45 days
    python3 src/ingestion/ingest_finra_short_interest.py --backfill-from 2024-01-01
    python3 src/ingestion/ingest_finra_short_interest.py --force          # re-pull present dates
"""
from __future__ import annotations

import argparse
import calendar
import io
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # invoked as `python3 src/ingestion/…`
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

MASTER_PATH = ROOT / 'data' / 'master' / 'short_interest.parquet'
URL_TMPL = 'https://cdn.finra.org/equity/otcmarket/biweekly/shrt{ymd}.csv'
KEY_COLS = ['settlement_date', 'ticker']
SOURCE = 'finra'
SLEEP_BETWEEN_FILES_S = 0.5
HOLIDAY_FALLBACK_BDAYS = 2   # 403 on a candidate → try this many earlier business days
DEFAULT_LOOKBACK_DAYS = 45

COLUMNS = [
    'settlement_date', 'ticker', 'issue_name', 'exchange', 'market_class',
    'short_interest', 'prev_short_interest', 'avg_daily_volume', 'days_to_cover',
    'change_pct', 'change_shares', 'split_flag', 'revision_flag', 'source', 'fetched_at',
]

_RAW_TO_COL = {
    'symbolCode': 'ticker',
    'issueName': 'issue_name',
    'issuerServicesGroupExchangeCode': 'exchange',
    'marketClassCode': 'market_class',
    'currentShortPositionQuantity': 'short_interest',
    'previousShortPositionQuantity': 'prev_short_interest',
    'stockSplitFlag': 'split_flag',
    'averageDailyVolumeQuantity': 'avg_daily_volume',
    'daysToCoverQuantity': 'days_to_cover',
    'revisionFlag': 'revision_flag',
    'changePercent': 'change_pct',
    'changePreviousNumber': 'change_shares',
    'settlementDate': 'settlement_date',
}
_INT_COLS = ['short_interest', 'prev_short_interest', 'avg_daily_volume', 'change_shares']
_FLOAT_COLS = ['days_to_cover', 'change_pct']
_STR_COLS = ['issue_name', 'exchange', 'market_class', 'split_flag', 'revision_flag']


# ── settlement-date candidates ───────────────────────────────────────────────

def _roll_back_weekend(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _prev_bday(d: date) -> date:
    return _roll_back_weekend(d - timedelta(days=1))


def candidate_settlement_dates(start: date, end: date) -> Iterator[date]:
    """The 15th and the last day of each month in [start, end], each rolled back
    over weekends (holidays are handled by the fetch loop's fallback)."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        for day in (15, calendar.monthrange(y, m)[1]):
            d = _roll_back_weekend(date(y, m, day))
            if start <= d <= end:
                yield d
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 30) -> tuple[int, bytes]:
    """(status, body). HTTP errors come back as (code, b''); network errors raise."""
    req = Request(url, headers={'User-Agent': 'openclaw-ingest/1.0 (+finra short interest)'})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, b''


def fetch_file(d: date) -> tuple[int, bytes]:
    return _http_get(URL_TMPL.format(ymd=d.strftime('%Y%m%d')))


# ── CSV → rows ───────────────────────────────────────────────────────────────

def rows_from_csv(body: bytes | str, *, fetched_at: pd.Timestamp) -> pd.DataFrame:
    """Parse one FINRA file into the master schema (COLUMNS order). Rows without
    a ticker are dropped; blank numerics stay null; tickers upper/stripped."""
    text = body.decode('utf-8-sig') if isinstance(body, bytes) else body
    raw = pd.read_csv(io.StringIO(text), sep='|', dtype=str, keep_default_na=False)
    df = raw.rename(columns=_RAW_TO_COL)
    for c in _RAW_TO_COL.values():
        if c not in df.columns:
            df[c] = ''
    df = df[list(_RAW_TO_COL.values())].copy()
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip()

    df['ticker'] = df['ticker'].str.upper()
    df = df[df['ticker'].ne('')].copy()

    df['settlement_date'] = pd.to_datetime(df['settlement_date'], errors='coerce').dt.date
    df = df[df['settlement_date'].notna()].copy()
    for c in _INT_COLS:
        df[c] = pd.to_numeric(df[c].replace('', None), errors='coerce').round().astype('Int64')
    for c in _FLOAT_COLS:
        df[c] = pd.to_numeric(df[c].replace('', None), errors='coerce').astype('float64')
    for c in _STR_COLS:
        df[c] = df[c].where(df[c].ne(''), None)
    df['source'] = SOURCE
    df['fetched_at'] = pd.Timestamp(fetched_at)
    return df[COLUMNS].reset_index(drop=True)


# ── master ───────────────────────────────────────────────────────────────────

def present_settlement_dates(master_path: Path = MASTER_PATH) -> set[date]:
    """Settlement dates already in the master (reads only the key column)."""
    master_path = Path(master_path)
    if not master_path.exists():
        return set()
    col = pq.read_table(master_path, columns=['settlement_date']).column('settlement_date')
    return {pd.Timestamp(v).date() for v in col.unique().to_pylist() if v is not None}


def merge_into_master(df: pd.DataFrame, *, master_path: Path = MASTER_PATH) -> dict:
    """UPSERT on (settlement_date, ticker). Returns counters."""
    from src.data.parquet_store import append_dedup, row_count

    before = row_count(master_path)
    df = df.drop_duplicates(subset=KEY_COLS, keep='last')
    after = append_dedup(master_path, df, KEY_COLS, mode='replace') if not df.empty else before
    new_rows = int(after - before)
    return {'fetched': int(len(df)), 'new_rows': new_rows,
            'replaced_rows': int(len(df) - new_rows), 'master_rows_after': int(after)}


# ── driver ───────────────────────────────────────────────────────────────────

def _attempt_chain(cand: date) -> list[date]:
    chain = [cand]
    for _ in range(HOLIDAY_FALLBACK_BDAYS):
        chain.append(_prev_bday(chain[-1]))
    return chain


def run(start: date, end: date, *, master_path: Path = MASTER_PATH, force: bool = False) -> tuple[int, dict]:
    stats = {'candidates': 0, 'files_fetched': 0, 'files_skipped': 0, 'files_unpublished': 0,
             'files_error': 0, 'fetched': 0, 'new_rows': 0, 'replaced_rows': 0,
             'master_rows_after': None}
    present = set() if force else present_settlement_dates(master_path)
    requests_made = 0
    first_request_raised = False

    for cand in candidate_settlement_dates(start, end):
        stats['candidates'] += 1
        chain = _attempt_chain(cand)
        hit = next((d for d in chain if d in present), None)
        if hit is not None:
            stats['files_skipped'] += 1
            logger.info('skip %s: settlement %s already in master', cand, hit)
            continue

        outcome = 'unpublished'
        for d in chain:
            if requests_made:
                time.sleep(SLEEP_BETWEEN_FILES_S)
            try:
                status, body = fetch_file(d)
            except Exception as exc:  # network-level failure
                if requests_made == 0:
                    first_request_raised = True
                requests_made += 1
                logger.warning('%s: request raised %s: %s', d, type(exc).__name__, exc)
                outcome = 'error'
                break
            requests_made += 1
            if status == 403:
                logger.info('%s: 403 (not a settlement date / not yet published)', d)
                continue
            if status != 200 or not body:
                logger.warning('%s: HTTP %s (%d bytes)', d, status, len(body))
                outcome = 'error'
                break
            df = rows_from_csv(body, fetched_at=pd.Timestamp.now(tz='UTC'))
            m = merge_into_master(df, master_path=master_path)
            present.update(df['settlement_date'].unique().tolist())
            for k in ('fetched', 'new_rows', 'replaced_rows'):
                stats[k] += m[k]
            stats['master_rows_after'] = m['master_rows_after']
            print(f"[finra-si] candidate={cand} url_date={d} settlement={sorted(df['settlement_date'].unique())} "
                  f"fetched={m['fetched']} new_rows={m['new_rows']} replaced_rows={m['replaced_rows']} "
                  f"master_rows_after={m['master_rows_after']}", flush=True)
            outcome = 'fetched'
            break
        stats[f'files_{outcome}'] += 1

    if first_request_raised:
        rc = 1
    else:
        attempted = stats['candidates'] - stats['files_skipped']
        rc = 1 if attempted and stats['files_error'] == attempted else 0
    if stats['master_rows_after'] is None:
        from src.data.parquet_store import row_count
        stats['master_rows_after'] = row_count(master_path)
    return rc, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--backfill-from', dest='start', help=f'YYYY-MM-DD (default: today − {DEFAULT_LOOKBACK_DAYS}d)')
    ap.add_argument('--until', dest='end', help='YYYY-MM-DD (default: today)')
    ap.add_argument('--force', action='store_true', help='re-fetch settlement dates already in the master')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    rc, s = run(start, end, master_path=MASTER_PATH, force=args.force)
    print(f"[finra-si] window={start}..{end} candidates={s['candidates']} files_fetched={s['files_fetched']} "
          f"files_skipped={s['files_skipped']} files_unpublished={s['files_unpublished']} "
          f"files_error={s['files_error']} fetched={s['fetched']} new_rows={s['new_rows']} "
          f"replaced_rows={s['replaced_rows']} master_rows_after={s['master_rows_after']} rc={rc}", flush=True)
    return rc


if __name__ == '__main__':
    sys.exit(main())
