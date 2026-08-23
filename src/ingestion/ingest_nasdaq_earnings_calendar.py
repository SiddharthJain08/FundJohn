"""Nasdaq keyless earnings calendar → data/master/earnings_calendar_nasdaq.parquet.

PURPOSE: a same-day cross-check of report TIMING (pre-market / after-hours)
and consensus (eps_forecast, num_estimates) for the pre-earnings strategies
(S_pre_earnings_vol_runup, S_ast_earnings_announcement_premium, S-HV17 …).
The MASTER `earnings.parquet` is fed from FMP `/stable/earnings-calendar`
(`ingest_earnings_master.py`, separate work). This file is a SECOND, independent
opinion on timing/consensus and is deliberately NOT merged into earnings.parquet.

Source (verified 2026-08-23): unofficial browser endpoint
    GET https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD
with browser-ish headers (User-Agent, Accept: application/json, Origin/Referer
www.nasdaq.com). JSON: {"data": {"rows": [{"lastYearRptDt": "8/26/2025",
"lastYearEPS": "$2.33", "time": "time-pre-market", "symbol": "BMO", "name": …,
"marketCap": "$121,887,293,613", "fiscalQuarterEnding": "Jul/2026",
"epsForecast": "$2.72", "noOfEsts": "3"}, …]}}. `time` ∈ {time-pre-market,
time-after-hours, time-not-supplied}. Money fields carry `$`, `,`, and
parenthesised negatives `$(0.12)`; blanks / "N/A" → null.

Brittle by nature: a couple of browser UAs are rotated, 1 s sleep between days;
403 / 429 / non-JSON / empty-JSON / request exceptions = "day unavailable"
(counted in days_failed, non-fatal). A 200 with rows: [] is a real empty day
(days_ok, 0 rows).

Master key (report_date, ticker); `parquet_store.append_dedup(mode='replace')`
— timing/consensus revisions overwrite, rows are never dropped (append-only).

Counters are the contract ("read the COUNTERS, not the exit code"):
  rc=1  the provider could not be read at all — ALL days in the window failed
  rc=0  otherwise, with days_ok / days_failed / rows / new_rows printed.

Usage:
    python3 src/ingestion/ingest_nasdaq_earnings_calendar.py                 # −3 … +45 business days
    python3 src/ingestion/ingest_nasdaq_earnings_calendar.py --days-ahead 10 --days-back 0
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd
import pyarrow as pa

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # invoked as `python3 src/ingestion/…`
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

MASTER_PATH = ROOT / 'data' / 'master' / 'earnings_calendar_nasdaq.parquet'
URL_TMPL = 'https://api.nasdaq.com/api/calendar/earnings?date={d}'
KEY_COLS = ['report_date', 'ticker']
SOURCE = 'nasdaq'
SLEEP_BETWEEN_DAYS_S = 1.0
DEFAULT_DAYS_AHEAD = 45
DEFAULT_DAYS_BACK = 3

USER_AGENTS = [
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
]

COLUMNS = [
    'report_date', 'ticker', 'company_name', 'report_time', 'eps_forecast', 'num_estimates',
    'last_year_eps', 'last_year_report_date', 'market_cap', 'fiscal_quarter_ending',
    'source', 'fetched_at',
]

_REPORT_TIME = {'time-pre-market': 'pre', 'time-after-hours': 'after'}
_MONEY_RE = re.compile(r'^\(?-?\$?\(?-?([0-9][0-9,]*\.?[0-9]*|\.[0-9]+)\)?$')


# ── field parsers ────────────────────────────────────────────────────────────

def parse_money(raw) -> float | None:
    """'$2.72' → 2.72; '$(0.12)' / '($0.12)' / '-$1.05' → negative; '$121,887,293,613' → float;
    blank / 'N/A' / unparseable → None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.upper() in {'N/A', 'NA', '-', '--'}:
        return None
    neg = s.startswith('-') or '(' in s
    m = _MONEY_RE.match(s)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(',', ''))
    except ValueError:
        return None
    return -v if neg else v


def parse_int(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(',', '')
    if not s or s.upper() in {'N/A', 'NA', '-', '--'}:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def map_report_time(raw) -> str:
    return _REPORT_TIME.get(str(raw or '').strip(), 'unknown')


def _parse_mdy(raw) -> date | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.upper() in {'N/A', 'NA'}:
        return None
    ts = pd.to_datetime(s, format='%m/%d/%Y', errors='coerce')
    return None if pd.isna(ts) else ts.date()


# ── payload → rows ───────────────────────────────────────────────────────────

def _empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype='object') for c in COLUMNS})


def rows_from_payload(body: bytes | str, *, report_date: date, fetched_at: pd.Timestamp) -> pd.DataFrame:
    """Map one day's JSON payload into the master schema. Raises ValueError when
    the body is not JSON (caller treats that as a failed day)."""
    text = body.decode('utf-8') if isinstance(body, bytes) else body
    payload = json.loads(text)
    rows = ((payload or {}).get('data') or {}).get('rows') or []
    out = []
    for r in rows:
        ticker = str(r.get('symbol') or '').strip().upper()
        if not ticker:
            continue
        out.append({
            'report_date': report_date,
            'ticker': ticker,
            'company_name': (str(r.get('name') or '').strip() or None),
            'report_time': map_report_time(r.get('time')),
            'eps_forecast': parse_money(r.get('epsForecast')),
            'num_estimates': parse_int(r.get('noOfEsts')),
            'last_year_eps': parse_money(r.get('lastYearEPS')),
            'last_year_report_date': _parse_mdy(r.get('lastYearRptDt')),
            'market_cap': parse_money(r.get('marketCap')),
            'fiscal_quarter_ending': (str(r.get('fiscalQuarterEnding') or '').strip() or None),
            'source': SOURCE,
            'fetched_at': pd.Timestamp(fetched_at),
        })
    if not out:
        return _empty()
    return _pin_types(pd.DataFrame(out, columns=COLUMNS))


def _pin_types(df: pd.DataFrame) -> pd.DataFrame:
    """Explicit arrow-facing dtypes so an all-null column in one day's batch (e.g.
    every lastYearRptDt blank) cannot land in the master as arrow `null` type."""
    for c in ('report_date', 'last_year_report_date'):
        df[c] = df[c].astype(pd.ArrowDtype(pa.date32()))
    for c in ('eps_forecast', 'last_year_eps', 'market_cap'):
        df[c] = df[c].astype('float64')
    df['num_estimates'] = df['num_estimates'].astype('Int64')
    for c in ('ticker', 'company_name', 'report_time', 'fiscal_quarter_ending', 'source'):
        df[c] = df[c].astype('string')
    return df


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _headers(ua: str) -> dict:
    return {
        'User-Agent': ua,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Origin': 'https://www.nasdaq.com',
        'Referer': 'https://www.nasdaq.com/',
    }


def _http_get(url: str, headers: dict, timeout: int = 30) -> tuple[int, bytes]:
    """(status, body). HTTP errors come back as (code, b''); network errors raise."""
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, b''


# ── business days ────────────────────────────────────────────────────────────

def business_days(today: date, *, days_back: int, days_ahead: int) -> list[date]:
    """`days_back` business days before `today`, `today` itself if a business day,
    and `days_ahead` business days after. Weekends only (holidays simply return
    an empty day from the API)."""
    back: list[date] = []
    d = today
    while len(back) < days_back:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            back.append(d)
    ahead: list[date] = []
    d = today
    while len(ahead) < days_ahead:
        d += timedelta(days=1)
        if d.weekday() < 5:
            ahead.append(d)
    mid = [today] if today.weekday() < 5 else []
    return list(reversed(back)) + mid + ahead


# ── master ───────────────────────────────────────────────────────────────────

def merge_into_master(df: pd.DataFrame, *, master_path: Path = MASTER_PATH) -> dict:
    from src.data.parquet_store import append_dedup, row_count

    before = row_count(master_path)
    df = df.drop_duplicates(subset=KEY_COLS, keep='last')
    after = append_dedup(master_path, df, KEY_COLS, mode='replace') if not df.empty else before
    new_rows = int(after - before)
    return {'rows': int(len(df)), 'new_rows': new_rows,
            'replaced_rows': int(len(df) - new_rows), 'master_rows_after': int(after)}


# ── driver ───────────────────────────────────────────────────────────────────

def run(today: date, *, days_back: int = DEFAULT_DAYS_BACK, days_ahead: int = DEFAULT_DAYS_AHEAD,
        master_path: Path = MASTER_PATH) -> tuple[int, dict]:
    stats = {'days': 0, 'days_ok': 0, 'days_failed': 0, 'rows': 0, 'new_rows': 0,
             'replaced_rows': 0, 'master_rows_after': None}
    days = business_days(today, days_back=days_back, days_ahead=days_ahead)
    stats['days'] = len(days)

    for i, d in enumerate(days):
        if i:
            time.sleep(SLEEP_BETWEEN_DAYS_S)
        ua = USER_AGENTS[i % len(USER_AGENTS)]
        url = URL_TMPL.format(d=d.isoformat())
        try:
            status, body = _http_get(url, _headers(ua))
        except Exception as exc:
            logger.warning('%s: request raised %s: %s', d, type(exc).__name__, exc)
            stats['days_failed'] += 1
            continue
        if status != 200 or not body:
            logger.warning('%s: HTTP %s (%d bytes) — day unavailable', d, status, len(body))
            stats['days_failed'] += 1
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            logger.warning('%s: non-JSON body (%d bytes) — day unavailable', d, len(body))
            stats['days_failed'] += 1
            continue
        data = (payload or {}).get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict) or 'rows' not in data:
            logger.warning('%s: empty JSON (no data.rows) — day unavailable', d)
            stats['days_failed'] += 1
            continue
        df = rows_from_payload(body, report_date=d, fetched_at=pd.Timestamp.now(tz='UTC'))
        m = merge_into_master(df, master_path=master_path)
        stats['days_ok'] += 1
        for k in ('rows', 'new_rows', 'replaced_rows'):
            stats[k] += m[k]
        stats['master_rows_after'] = m['master_rows_after']
        print(f"[nasdaq-ec] date={d} rows={m['rows']} new_rows={m['new_rows']} "
              f"replaced_rows={m['replaced_rows']} master_rows_after={m['master_rows_after']}", flush=True)

    if stats['master_rows_after'] is None:
        from src.data.parquet_store import row_count
        stats['master_rows_after'] = row_count(master_path)
    rc = 1 if days and stats['days_ok'] == 0 else 0
    return rc, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--days-ahead', type=int, default=DEFAULT_DAYS_AHEAD, help='business days after today')
    ap.add_argument('--days-back', type=int, default=DEFAULT_DAYS_BACK, help='business days before today (late changes)')
    ap.add_argument('--today', help='YYYY-MM-DD anchor (default: today)')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    today = date.fromisoformat(args.today) if args.today else date.today()
    rc, s = run(today, days_back=args.days_back, days_ahead=args.days_ahead, master_path=MASTER_PATH)
    print(f"[nasdaq-ec] today={today} days={s['days']} days_ok={s['days_ok']} days_failed={s['days_failed']} "
          f"rows={s['rows']} new_rows={s['new_rows']} replaced_rows={s['replaced_rows']} "
          f"master_rows_after={s['master_rows_after']} rc={rc}", flush=True)
    return rc


if __name__ == '__main__':
    sys.exit(main())
