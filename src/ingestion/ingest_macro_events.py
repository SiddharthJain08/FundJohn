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

CORRECTABILITY WITHOUT DELETE (fix round 1, T8 item 1): the master is
append-only, so a bad row can never be removed. `--deactivate EVENT=ISO`
appends the SAME (event, scheduled_at) key with active=False via the usual
mode='replace' upsert. A normal parsed row never writes 'active' explicitly
(it stays NULL, read as "active" by src/lib/macro_events.py); merge_into_master
only ever flips active back to True when the INCOMING row carries True
explicitly, which no parser here does — so a routine re-ingest of the same
event can never silently resurrect a deactivated one.

CALENDAR-COVERAGE GUARD (fix round 1, T8 item 2): before any write — a
source merge or a --deactivate — run() and run_deactivate() assert that
trading_calendar's master genuinely covers [min(parsed session dates) - 14d,
max + 14d] via the calendar's own covers()/first/last range, NOT via
is_session (which silently degrades to weekday-only arithmetic outside the
master's range). A too-narrow calendar refuses the write outright (rc 3): a
wrong session_date written under the weekday fallback could never be
corrected later (no DELETE).

Usage:
  python3 src/ingestion/ingest_macro_events.py                       # forward refresh
  python3 src/ingestion/ingest_macro_events.py --sources fed
  python3 src/ingestion/ingest_macro_events.py --from-file fed=/tmp/fomc.html
  python3 src/ingestion/ingest_macro_events.py --backfill --start-year 2017 --end-year 2027
  python3 src/ingestion/ingest_macro_events.py --deactivate FOMC_DECISION=2026-09-17T18:00:00+00:00
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
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

logger = logging.getLogger(__name__)

MASTER_PATH = ROOT / 'data' / 'master' / 'macro_events.parquet'
KEY_COLS = ['event', 'scheduled_at']
# NOTE: src/lib/macro_events.py holds its own copy of this list (not a
# shared import) — the two must be changed together. See T8 fix-round-1
# item 1. A parsed row never sets 'active' explicitly (stays NULL); only
# --deactivate writes an explicit False. See _frame()/run_deactivate().
COLUMNS = ['event', 'scheduled_at', 'session_date', 'source', 'ingested_at',
          'active']
SLEEP_BETWEEN_URLS_S = 1.0
_CALENDAR_MARGIN_DAYS = 14
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
# The gap between 'Minutes:' and '(Released ...)' tolerates short link text
# ('HTML', 'PDF', 'Implementation Note') but EXCLUDES DIGITS on purpose: a
# meeting whose minutes are still pending (no release date at all) must
# never let this gap skip forward across a later meeting's OWN day-range
# digits to steal ITS '(Released ...)' clause — that would both mis-pair the
# minutes date and erase the intervening meeting's day-range when the match
# is blanked out below (T8 fix-round-1 item 3a; see the furniture-trap test).
_FED_MINUTES_RE = re.compile(
    rf'Minutes\s*:?\s*[^\d]{{0,80}}?\(?\s*released\s+({_MONTH_RE})\s+(\d{{1,2}}),\s*(20\d{{2}})\s*\)?',
    re.I)
# The day-range group is MANDATORY: a bare 'Month D' (e.g. a page footer
# like 'Last Update: September 12, 2026') must never yield a decision row —
# only 'Month D-D' or 'Month D-Month D' does, and the decision day is always
# the SECOND day (T8 fix-round-1 item 3a).
_FED_MEETING_RE = re.compile(
    rf'({_MONTH_RE})\s+(\d{{1,2}})\s*[-–]\s*(?:({_MONTH_RE})\s+)?(\d{{1,2}})')


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
        df['active'] = pd.array([], dtype='boolean')
        return df
    df = df.drop_duplicates(subset=KEY_COLS, keep='last')
    df['event'] = df['event'].astype('string')
    df['source'] = df['source'].astype('string')
    df['scheduled_at'] = pd.to_datetime(df['scheduled_at'], utc=True)
    df['ingested_at'] = pd.to_datetime(df['ingested_at'], utc=True)
    # A normal parsed row never sets this key (see _row()), so it lands here
    # as NaN and casts to <NA> — "unset", read as active by the reader. Only
    # run_deactivate() ever puts an explicit False into the input rows.
    df['active'] = df['active'].astype('boolean')
    return df.reset_index(drop=True)


_CONTEXT_LOOKBACK_CHARS = 200  # every fixture's own record title sits well
# under 100 chars before its date; 200 is generous headroom for a real page
# while still excluding page furniture (title/nav/a stray mention elsewhere).


def _iter_datetimes(text: str):
    """Yield (context, naive_et_datetime) for every 'Month D, YYYY H:MM AM/PM'.

    `context` is the text between the PREVIOUS match and this one, which is the
    natural record boundary on a schedule page — a fixed-width lookback would
    let the previous row's title re-tag this row (the BEA advance/second
    estimate trap). The FIRST record has no previous match to bound it, so it
    is instead bounded to the last _CONTEXT_LOOKBACK_CHARS before its date —
    an unbounded ctx = text[0:m.start()] would let page furniture (a title, a
    nav breadcrumb) ahead of the very first record mis-tag it, an asymmetry
    every later record doesn't share (T8 fix-round-1 item 4c)."""
    prev_end = None
    for m in _DATETIME_RE.finditer(text):
        start = prev_end if prev_end is not None else max(0, m.start() - _CONTEXT_LOOKBACK_CHARS)
        ctx = text[start:m.start()]
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


def _from_file_matches(job_key: str, from_file_key: str) -> bool:
    """Exact key match, or <key><4-digit-year> — the backfill archive jobs
    ONLY (T8 fix-round-1 item 4b). A plain prefix match ('startswith') let an
    unrelated key like 'bls' silently swallow both 'bls_cpi' and
    'bls_empsit', which are separate single-release pages, not year-suffixed
    archives — this is stricter on purpose."""
    if job_key == from_file_key:
        return True
    if job_key.startswith(from_file_key):
        suffix = job_key[len(from_file_key):]
        return len(suffix) == 4 and suffix.isdigit()
    return False


# ── calendar-coverage guard ─────────────────────────────────────────────────

def _calendar_covers(session_dates) -> tuple:
    """(True, '') iff trading_calendar's master genuinely covers
    [min(session_dates) - 14d, max(session_dates) + 14d] via the calendar's
    OWN covers()/first/last range — NEVER via is_session, which silently
    degrades to weekday-only arithmetic outside the master's coverage. A
    write must refuse outright when this fails: a wrong session_date slipped
    in under the weekday fallback could never be corrected later (no DELETE
    on an append-only master). T8 fix-round-1 item 2."""
    dates = [d for d in session_dates if d is not None]
    if not dates:
        return True, ''
    lo = min(dates) - dt.timedelta(days=_CALENDAR_MARGIN_DAYS)
    hi = max(dates) + dt.timedelta(days=_CALENDAR_MARGIN_DAYS)
    from lib.trading_calendar import _calendar
    cal = _calendar()
    if cal is None or not cal.covers(lo) or not cal.covers(hi):
        have = f'{cal.first}..{cal.last}' if (cal is not None and cal.first) else 'no master'
        return False, (f'trading_calendar does not cover the required window '
                       f'[{lo}, {hi}] (have {have})')
    return True, ''


def _apply_sticky_deactivation(df: pd.DataFrame, master_path: Path) -> pd.DataFrame:
    """An append-only master has no DELETE, so an --deactivate active=False
    row is the only correction mechanism — a routine re-ingest of the SAME
    (event, scheduled_at) must not silently resurrect it. We only flip
    active back to True when the INCOMING row explicitly carries True; a
    normal parse never does (see _row()/_frame()), so this only ever bites
    a genuinely explicit reactivation, which nothing in this module's CLI
    currently issues. T8 fix-round-1 item 1 ('document the choice')."""
    master_path = Path(master_path)
    if not master_path.exists() or 'active' not in df.columns or df.empty:
        return df
    try:
        names = set(pq.ParquetFile(master_path).schema_arrow.names)
    except Exception:  # noqa: BLE001
        return df
    if 'active' not in names:
        return df
    existing = pd.read_parquet(master_path, columns=['event', 'scheduled_at', 'active'])
    inactive_mask = existing['active'].fillna(True) == False  # noqa: E712
    if not inactive_mask.any():
        return df
    inactive_keys = set(zip(existing.loc[inactive_mask, 'event'],
                            existing.loc[inactive_mask, 'scheduled_at']))
    df = df.copy()
    explicit_true = (df['active'] == True).fillna(False)  # noqa: E712
    for i in df.index:
        key = (df.at[i, 'event'], df.at[i, 'scheduled_at'])
        if key in inactive_keys and not bool(explicit_true.loc[i]):
            df.loc[i, 'active'] = False
    return df


def merge_into_master(df: pd.DataFrame, *, master_path: Path = MASTER_PATH) -> dict:
    from src.data.parquet_store import append_dedup, row_count

    before = row_count(master_path)
    if df.empty:
        return {'rows': 0, 'new_rows': 0, 'replaced_rows': 0,
                'master_rows_after': int(before)}
    df = _apply_sticky_deactivation(df, master_path)
    after = append_dedup(master_path, df, KEY_COLS, mode='replace')
    new_rows = int(after - before)
    return {'rows': int(len(df)), 'new_rows': new_rows,
            'replaced_rows': int(len(df) - new_rows),
            'master_rows_after': int(after)}


def run(sources, *, from_file=None, backfill: bool = False,
        start_year: int = 2017, end_year: int = 2027,
        master_path: Path = MASTER_PATH, dry_run: bool = False,
        allow_empty: bool = False) -> tuple:
    """Fetch + parse every job first (phase 1), THEN — once, over every
    non-empty parsed frame combined — run the calendar-coverage guard before
    any write (phase 2). This makes a multi-job run (e.g. --backfill)
    all-or-nothing: if the calendar can't be trusted for the parsed window,
    NOTHING gets written, not just the offending job (T8 fix-round-1 item 2;
    a deviation from the original brief's per-job fetch-then-immediately-
    merge loop, which could leave a partial write behind a mid-run failure).

    rc=2  a --from-file key matches no job in this run (checked BEFORE any
          fetch — never a silent live fetch, item 4b).
    rc=3  trading_calendar does not genuinely cover the parsed window — no
          write at all (item 2).
    rc=1  every job failed, OR any job parsed zero rows (soft failure,
          logged and counted separately as urls_empty) unless allow_empty.
    rc=0  otherwise.
    """
    from_file = dict(from_file or {})
    stats = {'urls': 0, 'urls_ok': 0, 'urls_failed': 0, 'urls_empty': 0,
             'rows': 0, 'new_rows': 0, 'replaced_rows': 0,
             'master_rows_after': None}
    jobs = _jobs(set(sources), backfill=backfill, start_year=start_year,
                 end_year=end_year)
    stats['urls'] = len(jobs)

    job_keys = [k for k, _, _ in jobs]
    unmatched = [k for k in from_file
                if not any(_from_file_matches(jk, k) for jk in job_keys)]
    if unmatched:
        logger.error('[macro-events] --from-file key(s) %s match no job in this run '
                     '(job keys: %s) — refusing rather than risk a silent live fetch',
                     unmatched, job_keys)
        return 2, stats

    fetched_at = pd.Timestamp.now(tz='UTC')
    parsed = []  # [(key, df)] — populated in phase 1, written in phase 2.

    for i, (key, url, parse) in enumerate(jobs):
        override = next((from_file[k] for k in from_file if _from_file_matches(key, k)), None)
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
        if df.empty:
            stats['urls_empty'] += 1
            logger.warning('%s: parsed 0 rows', key)
        parsed.append((key, df))

    if dry_run:
        # Print the ROWS, not just a count — a count alone can't be inspected
        # for the parser-hardening acceptance checks (T8 fix-round-1 item 4a).
        for key, df in parsed:
            print(f'[macro-events] DRY-RUN {key}: {len(df)} rows', flush=True)
            if not df.empty:
                print(df.to_string(), flush=True)
            stats['rows'] += len(df)
    else:
        non_empty = [df for _, df in parsed if not df.empty]
        if non_empty:
            all_dates = pd.concat([d['session_date'] for d in non_empty],
                                  ignore_index=True).tolist()
            ok, reason = _calendar_covers(all_dates)
            if not ok:
                logger.error('[macro-events] refusing to write: %s', reason)
                return 3, stats
        for key, df in parsed:
            m = merge_into_master(df, master_path=master_path)
            for k in ('rows', 'new_rows', 'replaced_rows'):
                stats[k] += m[k]
            stats['master_rows_after'] = m['master_rows_after']
            print(f"[macro-events] {key}: rows={m['rows']} new_rows={m['new_rows']} "
                  f"master_rows_after={m['master_rows_after']}", flush=True)

    if stats['master_rows_after'] is None and not dry_run:
        from src.data.parquet_store import row_count
        stats['master_rows_after'] = row_count(master_path)

    if jobs and stats['urls_ok'] == 0:
        rc = 1
    elif stats['urls_empty'] and not allow_empty:
        rc = 1
    else:
        rc = 0
    return rc, stats


def run_deactivate(specs, *, master_path: Path = MASTER_PATH, fetched_at=None,
                   dry_run: bool = False) -> tuple:
    """--deactivate EVENT=<ISO scheduled_at> (repeatable). Writes ONLY
    active=False rows — never source jobs — via the existing mode='replace'
    upsert on the (event, scheduled_at) key: never a DELETE. Same
    calendar-coverage guard as run() (item 2): refused (rc 3, no write) when
    trading_calendar does not genuinely cover the window.

    rc=3  calendar guard failed — no write.
    rc=4  the write went through, but at least one spec's (event,
          scheduled_at) matched NO existing master row — it was still
          appended (harmless on an append-only master, and it will bite if
          that exact event is ingested later), but this is almost always an
          operator error (ISO doesn't match the ingested scheduled_at to the
          second) and must be loud, not a silent apparent success. This rc
          is a fix-round-1 hardening beyond the letter of the brief.
    rc=0  every spec matched an existing row and was deactivated.
    """
    fetched_at = pd.Timestamp(fetched_at) if fetched_at is not None else pd.Timestamp.now(tz='UTC')
    rows = []
    for event, iso in specs:
        ts = pd.Timestamp(iso)
        ts = ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
        rows.append({'event': event, 'scheduled_at': ts,
                     'session_date': session_date_for(ts.to_pydatetime()),
                     'source': 'operator_deactivate', 'ingested_at': fetched_at,
                     'active': False})
    df = _frame(rows)

    if dry_run:
        print(f'[macro-events] DRY-RUN deactivate: {len(df)} row(s)', flush=True)
        if not df.empty:
            print(df.to_string(), flush=True)
        return 0, {'deactivated': int(len(df)), 'unmatched': [], 'master_rows_after': None}

    # Key pre-check FIRST (needs no calendar) so an operator with BOTH a
    # mistyped ISO and a too-narrow calendar sees both problems in one round
    # trip, rather than fixing the calendar only to then discover the typo.
    existing_keys = set()
    if Path(master_path).exists():
        try:
            existing = pd.read_parquet(master_path, columns=KEY_COLS)
            existing_keys = set(zip(existing['event'], existing['scheduled_at']))
        except Exception as e:  # noqa: BLE001
            logger.warning('[macro-events] could not pre-check existing keys (%s: %s)',
                           type(e).__name__, e)

    unmatched = [(e, ts) for e, ts in zip(df['event'], df['scheduled_at'])
                if (e, ts) not in existing_keys]
    for e, ts in unmatched:
        logger.error('[macro-events] --deactivate %s=%s matches NO existing master row — '
                     'writing an inert row anyway (never a delete), but this almost '
                     'certainly means the ISO does not exactly match the ingested '
                     'scheduled_at', e, ts.isoformat())
    unmatched_str = [f'{e}={ts.isoformat()}' for e, ts in unmatched]

    if not df.empty:
        ok, reason = _calendar_covers(df['session_date'])
        if not ok:
            logger.error('[macro-events] --deactivate refusing to write: %s', reason)
            return 3, {'deactivated': 0, 'unmatched': unmatched_str, 'master_rows_after': None}

    m = merge_into_master(df, master_path=master_path)
    print(f"[macro-events] deactivate: rows={m['rows']} new_rows={m['new_rows']} "
          f"master_rows_after={m['master_rows_after']}", flush=True)
    return (4 if unmatched else 0), {'deactivated': int(len(df)),
                                     'unmatched': unmatched_str,
                                     'master_rows_after': m['master_rows_after']}


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
    ap.add_argument('--allow-empty', action='store_true',
                    help='do not rc=1 when a source parses to zero rows')
    ap.add_argument('--deactivate', action='append', default=[],
                    metavar='EVENT=ISO',
                    help='append an active=False row for this (event, '
                         'scheduled_at) key — never a delete (repeatable). '
                         'Writes nothing else; refuses to combine with --backfill.')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    if args.deactivate:
        if args.backfill:
            logger.error('[macro-events] --deactivate refuses to combine with --backfill')
            return 2
        specs = []
        for item in args.deactivate:
            event, sep, iso = item.partition('=')
            if not sep or not event or not iso:
                logger.error('[macro-events] malformed --deactivate %r (want EVENT=ISO)', item)
                return 2
            specs.append((event, iso))
        rc, s = run_deactivate(specs, master_path=MASTER_PATH, dry_run=args.dry_run)
        print(f"[macro-events] deactivate={s.get('deactivated', 0)} "
              f"unmatched={s.get('unmatched', [])} "
              f"master_rows_after={s.get('master_rows_after')} rc={rc}", flush=True)
        return rc

    from_file = {}
    for item in args.from_file:
        k, _, v = item.partition('=')
        if k and v:
            from_file[k] = v

    rc, s = run([x.strip() for x in args.sources.split(',') if x.strip()],
                from_file=from_file, backfill=args.backfill,
                start_year=args.start_year, end_year=args.end_year,
                master_path=MASTER_PATH, dry_run=args.dry_run,
                allow_empty=args.allow_empty)
    print(f"[macro-events] urls={s['urls']} ok={s['urls_ok']} failed={s['urls_failed']} "
          f"empty={s['urls_empty']} rows={s['rows']} new_rows={s['new_rows']} "
          f"master_rows_after={s['master_rows_after']} rc={rc}", flush=True)
    return rc


if __name__ == '__main__':
    sys.exit(main())
