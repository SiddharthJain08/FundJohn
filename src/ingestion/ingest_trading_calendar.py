#!/usr/bin/env python3
"""Build / refresh data/master/trading_calendar.parquet from `alpaca calendar`.

`alpaca calendar --start --end` serves every NYSE session from 1970 through
2029 with open/close (early closes carry close=13:00) and exchange-declared
closures already removed (2025-01-09 day of mourning, Good Friday, …). One
call per year keeps each JSON payload small. Sessions the exchange drops after
we stored them are kept with active=false — the master never deletes rows.

Usage:
    python3 src/ingestion/ingest_trading_calendar.py [--start-year 1970] [--end-year <today+3>] [--path …]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.data.parquet_store import CALENDAR_KEYS, CALENDAR_PATH, append_dedup  # noqa: E402

log = logging.getLogger('ingest_trading_calendar')
_ALPACA_BIN = os.environ.get('ALPACA_CLI', '/root/go/bin/alpaca')
COLUMNS = ['date', 'open', 'close', 'session_open', 'session_close', 'settlement_date',
           'active', 'source', 'fetched_at']


def _run_cli(start: str, end: str) -> str:
    r = subprocess.run([_ALPACA_BIN, 'calendar', '--start', start, '--end', end],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f'alpaca calendar rc={r.returncode}: {r.stderr.strip()[:200]}')
    return r.stdout


def _d(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def fetch_year(year: int, run_cli=_run_cli) -> list[dict]:
    raw = run_cli(f'{year}-01-01', f'{year}-12-31')
    rows = json.loads(raw) if raw.strip() else []
    out = []
    for x in rows:
        d = _d(x.get('date'))
        if d is None:
            continue
        out.append({'date': d, 'open': str(x.get('open') or '09:30'), 'close': str(x.get('close') or '16:00'),
                    'session_open': str(x.get('session_open') or '0400'),
                    'session_close': str(x.get('session_close') or '2000'),
                    'settlement_date': _d(x.get('settlement_date'))})
    return out


def build_rows(years: list[int], run_cli=_run_cli) -> pd.DataFrame:
    stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    rows = []
    for y in years:
        for r in fetch_year(y, run_cli):
            rows.append({**r, 'active': True, 'source': 'alpaca', 'fetched_at': stamp})
    df = pd.DataFrame(rows, columns=COLUMNS)
    return df


def mark_removed(existing: pd.DataFrame, fetched: pd.DataFrame, years: list[int]) -> pd.DataFrame:
    """Rows of `existing` in the fetched years that the exchange no longer lists → active=False."""
    if existing is None or existing.empty:
        return pd.DataFrame(columns=COLUMNS)
    ex = existing.copy()
    ex['date'] = pd.to_datetime(ex['date']).dt.date
    in_years = ex[ex['date'].map(lambda d: d.year in set(years))]
    gone = in_years[~in_years['date'].isin(set(pd.to_datetime(fetched['date']).dt.date))].copy()
    gone['active'] = False
    return gone[COLUMNS]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start-year', type=int, default=1970)
    ap.add_argument('--end-year', type=int, default=dt.date.today().year + 3)
    ap.add_argument('--path', default=str(CALENDAR_PATH))
    a = ap.parse_args(argv)
    years = list(range(a.start_year, a.end_year + 1))
    path = Path(a.path)
    fetched = build_rows(years, _run_cli)
    if fetched.empty:
        log.error('no sessions fetched for %s..%s — master untouched', a.start_year, a.end_year)
        return 1
    existing = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
    removed = mark_removed(existing, fetched, years)
    df = pd.concat([fetched, removed], ignore_index=True)
    df['date'] = pd.to_datetime(df['date']).dt.date
    df['settlement_date'] = df['settlement_date'].map(lambda v: _d(v))
    total = append_dedup(path, df, CALENDAR_KEYS, mode='replace')
    print(f'[trading_calendar] years={years[0]}..{years[-1]} fetched={len(fetched):,} '
          f'deactivated={len(removed)} total_rows={total:,} path={path}', flush=True)
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    sys.exit(main())
