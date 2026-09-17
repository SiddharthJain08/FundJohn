#!/usr/bin/env python3
"""Keyless macro-event calendar ingest -> data/master/macro_events.parquet.

Spec: docs/specs/2026-09-12-quantdinger-adoptions-spec.md §3 C3 (ruling R3).
Append-only, dedup key (event, scheduled_at); the reader is src/lib/macro_events.py.

SOURCES (all keyless, all HTML):
  federalreserve.gov  FOMC_DECISION (statement day = the LAST day of each
                      meeting range, 14:00 ET) + FOMC_MINUTES (14:00 ET)
  bls.gov             CPI, NFP (Employment Situation) — 08:30 ET
  bea.gov             GDP_ADV (advance estimate only), PCE — 08:30 ET

PARSING STRATEGY: every parser runs over TAG-STRIPPED TEXT, never a DOM walk,
so markup churn on a government site does not silently zero the master. Probed
2026-09-13: the Fed page and the BEA schedule answer 200 text/html to a bare
HEAD; BLS answers 403 to a bare HEAD (Akamai) and needs browser headers, which
_headers() supplies.

THESE PARSERS WERE NOT VALIDATED AGAINST A LIVE FETCH. No live page was
fetched while writing this module — every fixture under
tests/fixtures/macro_events/ is hand-authored to the structural motifs of the
real pages (three `curl -I` probes only: Fed 200, BEA 200, BLS 403). Before
the timer is enabled, the operator must run the Step 12 validation gate
(`--dry-run` against the live URLs, or `--from-file` against a saved page) and
inspect the parsed rows; `--from-file SOURCE=PATH` exists precisely so a saved
page can stand in when a URL 404s, blocks, or the real markup diverges from
these fixtures.

Usage:
  python3 src/ingestion/ingest_macro_events.py                       # forward refresh
  python3 src/ingestion/ingest_macro_events.py --sources fed
  python3 src/ingestion/ingest_macro_events.py --from-file fed=/tmp/fomc.html
  python3 src/ingestion/ingest_macro_events.py --backfill --start-year 2017 --end-year 2027
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

logger = logging.getLogger(__name__)

MASTER_PATH = ROOT / 'data' / 'master' / 'macro_events.parquet'
KEY_COLS = ['event', 'scheduled_at']
COLUMNS = ['event', 'scheduled_at', 'session_date', 'source', 'ingested_at']
SLEEP_BETWEEN_URLS_S = 1.0
_ET = ZoneInfo('America/New_York')

# ── the one URL block (edit here, nowhere else) ─────────────────────────────
FED_CALENDAR_URL = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
FED_HISTORICAL_URL = 'https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm'
BLS_CPI_URL = 'https://www.bls.gov/schedule/news_release/cpi.htm'
BLS_EMPSIT_URL = 'https://www.bls.gov/schedule/news_release/empsit.htm'
BLS_ANNUAL_URL = 'https://www.bls.gov/schedule/news_release/{year}_sched.htm'
BEA_SCHEDULE_URL = 'https://www.bea.gov/news/schedule'

# NOTE: 'fed_historical' and 'bls_annual' hold `{year}`-templated URLs, not
# directly fetchable ones — a caller iterating .values() must .format(year=…)
# first (see _jobs() below for the only place that does).
SOURCE_URLS = {
    'fed': FED_CALENDAR_URL,
    'fed_historical': FED_HISTORICAL_URL,
    'bls_cpi': BLS_CPI_URL,
    'bls_empsit': BLS_EMPSIT_URL,
    'bls_annual': BLS_ANNUAL_URL,
    'bea': BEA_SCHEDULE_URL,
}

USER_AGENTS = [
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
]

BLS_TITLES = ((('consumer price index',), 'CPI'),
              (('employment situation',), 'NFP'))
BEA_TITLES = ((('gross domestic product', 'advance'), 'GDP_ADV'),
              (('personal income and outlays',), 'PCE'))

_MONTHS = {m.lower(): i for i, m in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
     'September', 'October', 'November', 'December'], start=1)}
_MONTH_RE = ('(?:January|February|March|April|May|June|July|August|September|'
             'October|November|December)')
_DATETIME_RE = re.compile(
    rf'({_MONTH_RE})\s+(\d{{1,2}}),\s*(20\d{{2}})\s+(\d{{1,2}}):(\d{{2}})\s*'
    r'([AaPp])\.?\s*[Mm]\.?')
_FED_YEAR_RE = re.compile(r'(20\d{2})\s+FOMC\s+Meeting')
_FED_MINUTES_RE = re.compile(
    rf'Minutes\s*:?\s*\(?\s*released\s+({_MONTH_RE})\s+(\d{{1,2}}),\s*(20\d{{2}})\s*\)?',
    re.I)
_FED_MEETING_RE = re.compile(
    rf'({_MONTH_RE})\s+(\d{{1,2}})(?:\s*[-–]\s*(?:({_MONTH_RE})\s+)?(\d{{1,2}}))?')


# ── text + time helpers ─────────────────────────────────────────────────────

def _text(html: str) -> str:
    """Tag-stripped, whitespace-collapsed page text."""
    txt = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', html or '')
    txt = re.sub(r'(?s)<[^>]+>', ' ', txt)
    for ent, rep in (('&nbsp;', ' '), ('&amp;', '&'), ('&#8211;', '-'),
                     ('&ndash;', '-'), ('&mdash;', '-'), ('&#8212;', '-')):
        txt = txt.replace(ent, rep)
    return re.sub(r'\s+', ' ', txt).strip()


def session_date_for(scheduled_at_utc) -> dt.date:
    """The NYSE session a release lands in: its ET calendar day when that day
    is a session, otherwise the next session (a holiday release is felt at the
    next open)."""
    from lib.trading_calendar import is_session, next_session
    ts = pd.Timestamp(scheduled_at_utc)
    et = ts.tz_convert(_ET) if ts.tzinfo else ts.tz_localize('UTC').tz_convert(_ET)
    d = et.date()
    return d if is_session(d) else next_session(d)


def _row(event: str, naive_et: dt.datetime, source: str, fetched_at) -> dict:
    utc = naive_et.replace(tzinfo=_ET).astimezone(dt.timezone.utc)
    return {'event': event, 'scheduled_at': pd.Timestamp(utc),
            'session_date': session_date_for(utc), 'source': source,
            'ingested_at': pd.Timestamp(fetched_at)}


def _frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLUMNS)
    if df.empty:
        return df
    df = df.drop_duplicates(subset=KEY_COLS, keep='last')
    df['event'] = df['event'].astype('string')
    df['source'] = df['source'].astype('string')
    df['scheduled_at'] = pd.to_datetime(df['scheduled_at'], utc=True)
    df['ingested_at'] = pd.to_datetime(df['ingested_at'], utc=True)
    return df.reset_index(drop=True)


def _iter_datetimes(text: str):
    """Yield (context, naive_et_datetime) for every 'Month D, YYYY H:MM AM/PM'.

    `context` is the text between the PREVIOUS match and this one, which is the
    natural record boundary on a schedule page — a fixed-width lookback would
    let the previous row's title re-tag this row (the BEA advance/second
    estimate trap)."""
    prev_end = 0
    for m in _DATETIME_RE.finditer(text):
        ctx = text[prev_end:m.start()]
        prev_end = m.end()
        hour, minute = int(m.group(4)), int(m.group(5))
        ap = m.group(6).lower()
        if ap == 'p' and hour != 12:
            hour += 12
        if ap == 'a' and hour == 12:
            hour = 0
        try:
            yield ctx, dt.datetime(int(m.group(3)), _MONTHS[m.group(1).lower()],
                                   int(m.group(2)), hour, minute)
        except (KeyError, ValueError):
            continue


# ── parsers ─────────────────────────────────────────────────────────────────

def parse_fed(html: str, *, fetched_at) -> pd.DataFrame:
    """FOMC decision days (last day of each meeting range, 14:00 ET) + minutes
    release dates (14:00 ET), anchored on the '<YYYY> FOMC Meetings' headings."""
    text = _text(html)
    anchors = [(int(m.group(1)), m.start()) for m in _FED_YEAR_RE.finditer(text)]
    rows = []
    for i, (year, pos) in enumerate(anchors):
        end = anchors[i + 1][1] if i + 1 < len(anchors) else len(text)
        span = text[pos:end]
        # Minutes first, then REMOVE them: '(released May 20, 2026)' would
        # otherwise parse as a May 20 meeting.
        for m in _FED_MINUTES_RE.finditer(span):
            try:
                rows.append(_row('FOMC_MINUTES',
                                 dt.datetime(int(m.group(3)),
                                             _MONTHS[m.group(1).lower()],
                                             int(m.group(2)), 14, 0),
                                 'federalreserve.gov', fetched_at))
            except (KeyError, ValueError):
                continue
        span = _FED_MINUTES_RE.sub(' ', span)
        for m in _FED_MEETING_RE.finditer(span):
            month_name = m.group(3) or m.group(1)
            day = int(m.group(4) or m.group(2))
            try:
                rows.append(_row('FOMC_DECISION',
                                 dt.datetime(year, _MONTHS[month_name.lower()],
                                             day, 14, 0),
                                 'federalreserve.gov', fetched_at))
            except (KeyError, ValueError):
                continue
    return _frame(rows)


def parse_titled(html: str, titles, source: str, *, fetched_at,
                 default_event=None) -> pd.DataFrame:
    """Schedule pages whose rows are '<title> <Month D, YYYY> <H:MM AM>'.

    `titles` is ((required_substrings, event), ...) matched case-insensitively
    against the record's own context. `default_event` covers single-release
    pages (bls.gov/schedule/news_release/cpi.htm) whose rows carry no title."""
    rows = []
    for ctx, naive in _iter_datetimes(_text(html)):
        low = ctx.lower()
        event = None
        for needles, name in (titles or ()):
            if all(n in low for n in needles):
                event = name
                break
        if event is None:
            event = default_event
        if event is None:
            continue
        rows.append(_row(event, naive, source, fetched_at))
    return _frame(rows)


# ── HTTP ────────────────────────────────────────────────────────────────────

def _headers(ua: str) -> dict:
    return {'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9'}


def _http_get(url: str, headers: dict, timeout: int = 30) -> tuple:
    """(status, text). HTTP errors come back as (code, ''); network errors raise."""
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except HTTPError as e:
        return e.code, ''


# ── plan of work ────────────────────────────────────────────────────────────

def _jobs(sources, *, backfill: bool, start_year: int, end_year: int) -> list:
    """[(source_key, url, parse_callable)]. One place decides what gets fetched."""
    out = []
    if 'fed' in sources:
        out.append(('fed', FED_CALENDAR_URL,
                    lambda h, ts: parse_fed(h, fetched_at=ts)))
        if backfill:
            for y in range(start_year, end_year + 1):
                out.append((f'fed{y}', FED_HISTORICAL_URL.format(year=y),
                            lambda h, ts: parse_fed(h, fetched_at=ts)))
    if 'bls' in sources:
        out.append(('bls_cpi', BLS_CPI_URL,
                    lambda h, ts: parse_titled(h, (), 'bls.gov', fetched_at=ts,
                                               default_event='CPI')))
        out.append(('bls_empsit', BLS_EMPSIT_URL,
                    lambda h, ts: parse_titled(h, (), 'bls.gov', fetched_at=ts,
                                               default_event='NFP')))
        if backfill:
            for y in range(start_year, end_year + 1):
                out.append((f'bls{y}', BLS_ANNUAL_URL.format(year=y),
                            lambda h, ts: parse_titled(h, BLS_TITLES, 'bls.gov',
                                                       fetched_at=ts)))
    if 'bea' in sources:
        out.append(('bea', BEA_SCHEDULE_URL,
                    lambda h, ts: parse_titled(h, BEA_TITLES, 'bea.gov',
                                               fetched_at=ts)))
    return out


def merge_into_master(df: pd.DataFrame, *, master_path: Path = MASTER_PATH) -> dict:
    from src.data.parquet_store import append_dedup, row_count

    before = row_count(master_path)
    after = append_dedup(master_path, df, KEY_COLS, mode='replace') \
        if not df.empty else before
    new_rows = int(after - before)
    return {'rows': int(len(df)), 'new_rows': new_rows,
            'replaced_rows': int(len(df) - new_rows),
            'master_rows_after': int(after)}


def run(sources, *, from_file=None, backfill: bool = False,
        start_year: int = 2017, end_year: int = 2027,
        master_path: Path = MASTER_PATH, dry_run: bool = False) -> tuple:
    """Fetch + parse + merge. A failed URL is COUNTED and skipped; rc=1 only
    when every job failed (mirrors ingest_nasdaq_earnings_calendar)."""
    from_file = dict(from_file or {})
    stats = {'urls': 0, 'urls_ok': 0, 'urls_failed': 0, 'rows': 0,
             'new_rows': 0, 'replaced_rows': 0, 'master_rows_after': None}
    jobs = _jobs(set(sources), backfill=backfill, start_year=start_year,
                 end_year=end_year)
    # An explicit --from-file key replaces every job whose key starts with it,
    # so `--from-file fed=...` also satisfies the backfill's fed<year> jobs.
    stats['urls'] = len(jobs)
    fetched_at = pd.Timestamp.now(tz='UTC')

    for i, (key, url, parse) in enumerate(jobs):
        override = next((p for k, p in from_file.items() if key.startswith(k)), None)
        html = None
        if override:
            try:
                html = Path(override).read_text()
            except Exception as e:  # noqa: BLE001
                logger.warning('%s: %s unreadable (%s)', key, override, e)
        else:
            if i:
                time.sleep(SLEEP_BETWEEN_URLS_S)
            try:
                status, body = _http_get(url, _headers(USER_AGENTS[i % len(USER_AGENTS)]))
            except Exception as e:  # noqa: BLE001
                logger.warning('%s: request raised %s: %s', key, type(e).__name__, e)
                status, body = 0, ''
            if status == 200 and body:
                html = body
            else:
                logger.warning('%s: HTTP %s (%d bytes) — source unavailable',
                               key, status, len(body or ''))
        if html is None:
            stats['urls_failed'] += 1
            continue

        try:
            df = parse(html, fetched_at)
        except Exception as e:  # noqa: BLE001
            logger.warning('%s: parse raised %s: %s', key, type(e).__name__, e)
            stats['urls_failed'] += 1
            continue
        stats['urls_ok'] += 1
        if dry_run:
            print(f'[macro-events] DRY-RUN {key}: {len(df)} rows', flush=True)
            stats['rows'] += len(df)
            continue
        m = merge_into_master(df, master_path=master_path)
        for k in ('rows', 'new_rows', 'replaced_rows'):
            stats[k] += m[k]
        stats['master_rows_after'] = m['master_rows_after']
        print(f"[macro-events] {key}: rows={m['rows']} new_rows={m['new_rows']} "
              f"master_rows_after={m['master_rows_after']}", flush=True)

    if stats['master_rows_after'] is None and not dry_run:
        from src.data.parquet_store import row_count
        stats['master_rows_after'] = row_count(master_path)
    rc = 1 if jobs and stats['urls_ok'] == 0 else 0
    return rc, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--sources', default='fed,bls,bea',
                    help='comma-separated subset of fed,bls,bea')
    ap.add_argument('--from-file', action='append', default=[],
                    metavar='KEY=PATH',
                    help='parse a saved page instead of fetching (repeatable)')
    ap.add_argument('--backfill', action='store_true',
                    help='also fetch the per-year archive pages')
    ap.add_argument('--start-year', type=int, default=2017)
    ap.add_argument('--end-year', type=int, default=2027)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    from_file = {}
    for item in args.from_file:
        k, _, v = item.partition('=')
        if k and v:
            from_file[k] = v

    rc, s = run([x.strip() for x in args.sources.split(',') if x.strip()],
                from_file=from_file, backfill=args.backfill,
                start_year=args.start_year, end_year=args.end_year,
                master_path=MASTER_PATH, dry_run=args.dry_run)
    print(f"[macro-events] urls={s['urls']} ok={s['urls_ok']} failed={s['urls_failed']} "
          f"rows={s['rows']} new_rows={s['new_rows']} "
          f"master_rows_after={s['master_rows_after']} rc={rc}", flush=True)
    return rc


if __name__ == '__main__':
    sys.exit(main())
