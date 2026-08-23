"""
ingest_earnings_master.py
=========================
Keep data/master/earnings.parquet fed. This is the file every earnings
consumer actually reads (engine.py earnings_dte, S_earnings_sue_pead,
S_pre_earnings_vol_runup, S_ast_earnings_announcement_premium) — and it
froze at last_updated 2026-04-30 because its only refresh path was
FMP-based dead code with zero callers (2026-08-06 remediation spec §3),
then froze AGAIN at 2026-08-06 because the yfinance replacement feed has
returned "Too Many Requests" on every daily run since (D2, 2026-08-23).

Sources, in priority order
--------------------------
1. FMP `/stable/earnings-calendar` (PRIMARY since 2026-08-23). ONE bulk
   sweep per run over [today − lookback, today + horizon] (defaults 10 /
   120 days) in contiguous 7-day windows (FMP caps the span per call; ≈19
   calls at ~1 s pacing, 429 → backoff honouring Retry-After). Every row
   carries {symbol, date, epsActual, epsEstimated, revenueActual,
   revenueEstimated, lastUpdated}, so the same sweep yields the forward
   calendar for the whole market AND the reported actuals for events in
   the trailing window. Rows are restricted to tickers already in the
   master OR in the active universe (`universe_config.active = TRUE`, or
   --universe-file). NOTE: only FMP's legacy `/api/v3/` earnings endpoints
   403 ("Legacy Endpoint") on the current key; `/stable/` works.

2. yfinance (FALLBACK — taken only when FMP fails outright: key missing,
   every window erroring, or an empty payload). Master rows whose date
   just passed with eps_actual still NaN identify tickers that reported
   recently; those get one Ticker.earnings_dates call each via the
   cboe_vol_indices gateway (the sole allowed yfinance importer).

Both feeds are merged by `merge_rows`: (ticker, date) matched exactly,
else within ±1 day (bmo/amc date skew between providers). Matched rows
are NaN-filled in place (actuals, revenue, missing estimates); an
UNREPORTED forward row also takes the latest consensus estimate; a
REPORTED row's estimate is never rewritten (SUE reproducibility). Unmatched
rows are appended. Rows are never dropped — the master's legacy duplicate
keys survive untouched.

Also every run: earnings_calendar.parquet (still written daily by
ingest_earnings_calendar.py via yfinance) is merged forward as before —
it is a local file, so the master no longer DEPENDS on yfinance succeeding.

--backfill sweeps an explicit ticker set (or the full prices universe)
through the yfinance actuals fetch — legacy repair path; prefer
`--lookback-days N` on the FMP sweep for recent gaps.

Append-only contract (CLAUDE.md): rows and columns are only ever added or
NaN-filled; nothing is deleted. Writes are atomic (tmp + os.replace under
the parquet_store file lock). --dry-run runs the full fetch + merge +
tmp serialisation and skips ONLY the final os.replace.

Exit status: 0 when FMP or the fallback delivered rows (counters printed
either way — read the counters, not the exit code); 1 when BOTH failed
or the merge would shrink the master.

Usage
-----
    python3 src/ingestion/ingest_earnings_master.py               # daily
    python3 src/ingestion/ingest_earnings_master.py --dry-run
    python3 src/ingestion/ingest_earnings_master.py --lookback-days 20
    python3 src/ingestion/ingest_earnings_master.py --backfill --limit 100
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent.parent
MASTER_PATH = ROOT / 'data' / 'master' / 'earnings.parquet'
CALENDAR_PATH = ROOT / 'data' / 'master' / 'earnings_calendar.parquet'
PRICES_PATH = ROOT / 'data' / 'master' / 'prices.parquet'

MASTER_COLUMNS = ['ticker', 'date', 'eps_actual', 'eps_estimated',
                  'revenue_actual', 'revenue_estimated', 'last_updated']
VALUE_COLUMNS = ['eps_actual', 'eps_estimated', 'revenue_actual',
                 'revenue_estimated']
ESTIMATE_COLUMNS = ('eps_estimated', 'revenue_estimated')

# How far back an unreported (eps_actual NaN) past event still triggers a
# yfinance actuals fetch (fallback path). Covers a long weekend + a few
# missed runs.
ACTUALS_LOOKBACK_DAYS = 14

# FMP /stable bulk calendar. Span per call is capped server-side; OpenBB's
# FMP fetcher uses 7-day windows, so do we.
FMP_CALENDAR_URL = 'https://financialmodelingprep.com/stable/earnings-calendar'
FMP_WINDOW_DAYS = 7
FMP_LOOKBACK_DAYS = 10
FMP_HORIZON_DAYS = 120
FMP_TIMEOUT_S = 30
FMP_MAX_ATTEMPTS = 4
# Seconds between window calls. Same posture as backfillers/fmp.py
# MIN_BACKFILL_CALLS after the 2026-07-03 429 storm.
FMP_PACE_S = 1.0

FMP_FIELD_MAP = {
    'symbol':           'ticker',
    'date':             'date',
    'epsActual':        'eps_actual',
    'epsEstimated':     'eps_estimated',
    'revenueActual':    'revenue_actual',
    'revenueEstimated': 'revenue_estimated',
}


class FmpUnavailable(RuntimeError):
    """FMP could not feed this run (no key / every window failed / empty)."""


def _today() -> date:
    return date.today()


def _record_call(provider: str, endpoint: str, *, success: bool,
                 error: str | None = None) -> None:
    """Best-effort data_provider_health upsert (dashboard tile). Never raises."""
    try:
        sys.path.insert(0, str(ROOT))
        from src.maintenance.provider_health import record
        record(provider, endpoint, success=success, error=error)
    except Exception:
        pass


def _load_master() -> pd.DataFrame:
    if MASTER_PATH.exists():
        df = pd.read_parquet(MASTER_PATH)
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        return df
    return pd.DataFrame(columns=MASTER_COLUMNS)


# ── Forward merge from the local yfinance calendar (unchanged) ──────────────

def forward_rows(master: pd.DataFrame, today: date) -> pd.DataFrame:
    """Calendar rows the master lacks, shaped to the master schema."""
    if not CALENDAR_PATH.exists():
        return pd.DataFrame(columns=MASTER_COLUMNS)
    cal = pd.read_parquet(CALENDAR_PATH)
    if cal.empty:
        return pd.DataFrame(columns=MASTER_COLUMNS)
    cal = cal.rename(columns={'next_earnings_date': 'date',
                              'eps_estimate': 'eps_estimated',
                              'revenue_estimate': 'revenue_estimated'})
    cal['date'] = pd.to_datetime(cal['date'], errors='coerce')
    cal = cal.dropna(subset=['ticker', 'date'])
    have = set(zip(master['ticker'], master['date']))
    fresh = cal[[not (t, d) in have
                 for t, d in zip(cal['ticker'], cal['date'])]].copy()
    if fresh.empty:
        return pd.DataFrame(columns=MASTER_COLUMNS)
    fresh['eps_actual'] = float('nan')
    fresh['revenue_actual'] = float('nan')
    fresh['last_updated'] = today.isoformat()
    return fresh[MASTER_COLUMNS]


# ── FMP primary feed ────────────────────────────────────────────────────────

def fmp_windows(start: date, end: date,
                window_days: int = FMP_WINDOW_DAYS) -> list[tuple[date, date]]:
    """Contiguous inclusive [a, b] windows covering [start, end] exactly:
    each spans at most `window_days` calendar days, the next starts the day
    after the previous ends, the last is clipped to `end`."""
    if end < start:
        raise ValueError(f'end {end} < start {start}')
    out = []
    a = start
    while a <= end:
        b = min(a + timedelta(days=window_days - 1), end)
        out.append((a, b))
        a = b + timedelta(days=1)
    return out


def _fmp_get_window(a: date, b: date, api_key: str, *,
                    timeout: int = FMP_TIMEOUT_S,
                    max_attempts: int = FMP_MAX_ATTEMPTS) -> list:
    """One window → list of FMP rows. Retries 429 (Retry-After honoured,
    else exponential backoff). Any other non-200 raises RuntimeError."""
    params = {'from': a.isoformat(), 'to': b.isoformat(), 'apikey': api_key}
    last_err = 'no attempt made'
    for attempt in range(1, max_attempts + 1):
        r = requests.get(FMP_CALENDAR_URL, params=params, timeout=timeout)
        if r.status_code == 429:
            ra = r.headers.get('Retry-After')
            try:
                wait = float(ra) if ra else 2.0 ** attempt
            except ValueError:
                wait = 2.0 ** attempt
            wait = min(max(wait, 1.0), 60.0)
            last_err = f'HTTP 429 (attempt {attempt}/{max_attempts}, slept {wait:.0f}s)'
            print(f'  [fmp] {a}..{b}: {last_err}', file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code != 200:
            raise RuntimeError(f'HTTP {r.status_code}: {str(r.text)[:160]}')
        data = r.json()
        if not isinstance(data, list):
            raise RuntimeError(f'unexpected payload type {type(data).__name__}: '
                               f'{str(data)[:160]}')
        return data
    raise RuntimeError(last_err)


def fetch_fmp_calendar(start: date, end: date, api_key: str, *,
                       pace_s: float = FMP_PACE_S,
                       window_days: int = FMP_WINDOW_DAYS,
                       timeout: int = FMP_TIMEOUT_S
                       ) -> tuple[list[dict], int, int]:
    """Bulk sweep of /stable/earnings-calendar over [start, end].

    Returns (raw_rows, n_windows_ok, n_windows_failed). A failed window is
    counted and skipped — partial coverage beats none. Raises FmpUnavailable
    when there is no key or EVERY window failed (caller falls back).
    """
    if not api_key:
        raise FmpUnavailable('FMP_API_KEY not set')
    rows: list[dict] = []
    n_ok = n_fail = 0
    wins = fmp_windows(start, end, window_days)
    for i, (a, b) in enumerate(wins):
        try:
            data = _fmp_get_window(a, b, api_key, timeout=timeout)
            rows.extend(data)
            n_ok += 1
            _record_call('fmp', 'earnings_calendar', success=True)
        except Exception as e:   # network, HTTP, JSON — count, continue
            n_fail += 1
            print(f'  [fmp] window {a}..{b} FAILED: {e}', file=sys.stderr)
            _record_call('fmp', 'earnings_calendar', success=False,
                         error=str(e)[:200])
        if pace_s and i < len(wins) - 1:
            time.sleep(pace_s)
    if n_ok == 0:
        raise FmpUnavailable(f'all {n_fail} FMP windows failed')
    return rows, n_ok, n_fail


def fmp_rows_to_master(raw, today: date) -> pd.DataFrame:
    """FMP payload rows → master schema. Drops rows without a usable
    ticker/date, normalises share-class dots to the master's '-' convention,
    dedups on (ticker, date) keeping the LAST occurrence (FMP's lastUpdated
    ordering within a window is newest-last). last_updated is OUR touch
    stamp (today), not FMP's lastUpdated — master_freshness reads it."""
    df = pd.DataFrame(list(raw) if not isinstance(raw, pd.DataFrame) else raw)
    if df.empty:
        return pd.DataFrame(columns=MASTER_COLUMNS)
    for src in FMP_FIELD_MAP:
        if src not in df.columns:
            df[src] = None
    df = df[list(FMP_FIELD_MAP)].rename(columns=FMP_FIELD_MAP)
    df['ticker'] = df['ticker'].map(
        lambda s: None if (s is None or (not isinstance(s, str) and pd.isna(s)))
        else str(s).strip().upper().replace('.', '-'))
    df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.normalize()
    df = df[df['ticker'].notna() & (df['ticker'] != '') & df['date'].notna()]
    for c in VALUE_COLUMNS:
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('float64')
    df = df.drop_duplicates(subset=['ticker', 'date'], keep='last')
    df['last_updated'] = today.isoformat()
    return df[MASTER_COLUMNS].reset_index(drop=True)


def _universe_file_tickers(path: str | None) -> set[str]:
    if not path or not Path(path).exists():
        return set()
    with open(path) as f:
        return {ln.strip() for ln in f if ln.strip() and not ln.startswith('#')}


def _active_universe() -> list[str]:
    """universe_config.active = TRUE (mirrors backfillers/fmp.py). Returns []
    when the DB is unreachable — the master's own ticker set still applies;
    we deliberately do NOT fall back to scanning prices.parquet here."""
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return []
    try:
        import psycopg2
        conn = psycopg2.connect(uri)
        try:
            cur = conn.cursor()
            cur.execute('SELECT DISTINCT ticker FROM universe_config '
                        'WHERE active = TRUE ORDER BY ticker')
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f'  [fmp] universe_config unreachable ({e}); '
              f'restricting to master tickers', file=sys.stderr)
        return []


def filter_universe(rows: pd.DataFrame,
                    allowed: set[str]) -> tuple[pd.DataFrame, int]:
    """Keep rows whose ticker is allowed. Returns (kept, n_dropped)."""
    if rows.empty:
        return rows, 0
    keep = rows['ticker'].isin(allowed)
    return rows[keep].reset_index(drop=True), int((~keep).sum())


# ── Merge (shared by FMP and yfinance paths) ────────────────────────────────

def merge_rows(master: pd.DataFrame, rows: pd.DataFrame,
               today: date) -> tuple[pd.DataFrame, dict]:
    """Merge provider rows into the master. Never drops a row.

    Match on (ticker, date) exactly, else the nearest master row for that
    ticker within ±1 day (bmo/amc skew). On a match: NaN fields are filled
    from non-null incoming values; an UNREPORTED row dated today-or-later
    also takes a revised estimate; a reported row's estimate is frozen.
    No match: append when any value field is non-null.

    Returns (new_master, stats) with stats keys rows_new, rows_updated,
    actuals_filled, tickers_touched (set).
    """
    stats = {'rows_new': 0, 'rows_updated': 0, 'actuals_filled': 0,
             'tickers_touched': set()}
    if rows is None or rows.empty:
        return master, stats
    rows = rows.copy()
    rows['date'] = pd.to_datetime(rows['date'], errors='coerce')
    rows = rows.dropna(subset=['ticker', 'date'])
    for c in VALUE_COLUMNS:
        if c not in rows.columns:
            rows[c] = float('nan')

    master = master.copy()
    for c in VALUE_COLUMNS:
        if c not in master.columns:
            master[c] = float('nan')
        master[c] = master[c].astype('float64')
    if 'last_updated' not in master.columns:
        master['last_updated'] = None
    if 'date' in master.columns:
        master['date'] = pd.to_datetime(master['date'], errors='coerce')

    today_ts = pd.Timestamp(today)
    stamp = today.isoformat()
    one_day = pd.Timedelta(days=1)
    dates_by_ticker = {t: g['date'] for t, g in master.groupby('ticker')}
    appended = []
    for row in rows.itertuples(index=False):
        t, d = row.ticker, row.date
        idx = None
        ser = dates_by_ticker.get(t)
        if ser is not None:
            diff = (ser - d).abs()
            exact = diff.index[diff == pd.Timedelta(0)]
            if len(exact):
                idx = exact[0]
            else:
                near = diff[diff <= one_day]
                if not near.empty:
                    idx = near.idxmin()
        if idx is not None:
            changed = False
            reported = pd.notna(master.at[idx, 'eps_actual'])
            for c in VALUE_COLUMNS:
                v = getattr(row, c)
                if pd.isna(v):
                    continue
                cur = master.at[idx, c]
                if pd.isna(cur):
                    master.at[idx, c] = v
                    changed = True
                    if c == 'eps_actual':
                        stats['actuals_filled'] += 1
                elif (c in ESTIMATE_COLUMNS and not reported
                      and master.at[idx, 'date'] >= today_ts and cur != v):
                    master.at[idx, c] = v
                    changed = True
            if changed:
                master.at[idx, 'last_updated'] = stamp
                stats['rows_updated'] += 1
                stats['tickers_touched'].add(t)
        elif any(pd.notna(getattr(row, c)) for c in VALUE_COLUMNS):
            appended.append({
                'ticker': t, 'date': d,
                **{c: getattr(row, c) for c in VALUE_COLUMNS},
                'last_updated': stamp,
            })
            stats['rows_new'] += 1
            stats['tickers_touched'].add(t)
            if pd.notna(row.eps_actual):
                stats['actuals_filled'] += 1
    if appended:
        master = pd.concat([master, pd.DataFrame(appended)[MASTER_COLUMNS]],
                           ignore_index=True)
    return master, stats


# ── yfinance fallback ───────────────────────────────────────────────────────

def tickers_needing_actuals(master: pd.DataFrame, today: date) -> list[str]:
    """Tickers with a just-passed event still missing its reported EPS."""
    if master.empty:
        return []
    lo = pd.Timestamp(today - timedelta(days=ACTUALS_LOOKBACK_DAYS))
    hi = pd.Timestamp(today)  # exclusive: today's event reports amc at best
    win = master[(master['date'] >= lo) & (master['date'] < hi)
                 & (master['eps_actual'].isna())]
    return sorted(win['ticker'].unique())


def apply_actuals(master: pd.DataFrame, actuals: pd.DataFrame,
                  today: date) -> tuple[pd.DataFrame, int, int]:
    """yfinance actuals → master via merge_rows. Returns
    (new_master, n_filled, n_appended). Never drops a row."""
    if actuals is None or actuals.empty:
        return master, 0, 0
    out, stats = merge_rows(master, actuals, today)
    return out, stats['rows_updated'], stats['rows_new']


def _fetch_actuals(tickers: list[str], throttle_s: float) -> pd.DataFrame:
    sys.path.insert(0, str(ROOT))
    from src.ingestion.cboe_vol_indices import get_earnings_history
    return get_earnings_history(tickers, throttle_s=throttle_s)


# ── Write ───────────────────────────────────────────────────────────────────

def _atomic_write(df: pd.DataFrame, *, dry_run: bool = False) -> Path:
    """Serialise to {master}.tmp under the parquet_store lock, then
    os.replace → the master. --dry-run does everything except the replace
    (the tmp is removed) so a dry run exercises the real write path."""
    sys.path.insert(0, str(ROOT))
    from src.data import parquet_store as ps
    df = df.sort_values(['ticker', 'date']).reset_index(drop=True)
    tmp = Path(str(MASTER_PATH) + '.tmp')
    with ps._file_lock(MASTER_PATH):
        if dry_run:
            try:
                MASTER_PATH.parent.mkdir(parents=True, exist_ok=True)
                ps.pq.write_table(
                    ps.pa.Table.from_pandas(df, preserve_index=False),
                    tmp, compression='snappy')
                n = ps.pq.read_metadata(tmp).num_rows
                print(f'  --dry-run: serialised {n} rows to {tmp.name}; '
                      f'os.replace SKIPPED')
            finally:
                if tmp.exists():
                    tmp.unlink()
            return MASTER_PATH
        ps._atomic_write(MASTER_PATH, df)
    return MASTER_PATH


# ── Main ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true',
                   help='fetch + merge + serialise tmp, skip only os.replace')
    p.add_argument('--lookback-days', type=int, default=FMP_LOOKBACK_DAYS,
                   help=f'FMP sweep start = today − N (default {FMP_LOOKBACK_DAYS})')
    p.add_argument('--horizon-days', type=int, default=FMP_HORIZON_DAYS,
                   help=f'FMP sweep end = today + N (default {FMP_HORIZON_DAYS})')
    p.add_argument('--universe-file', default=None,
                   help='txt file of extra tickers allowed into the master '
                        '(default allowed = master tickers ∪ universe_config.active)')
    p.add_argument('--pace', type=float, default=FMP_PACE_S,
                   help=f'seconds between FMP window calls (default {FMP_PACE_S})')
    p.add_argument('--backfill', action='store_true',
                   help='yfinance actuals sweep over --tickers/--limit of the '
                        'prices universe instead of the FMP sweep')
    p.add_argument('--tickers', default=None,
                   help='comma-separated explicit ticker set (backfill)')
    p.add_argument('--limit', type=int, default=None,
                   help='cap ticker count (backfill debug aid)')
    p.add_argument('--throttle', type=float, default=0.4,
                   help='seconds between yfinance ticker calls (fallback/backfill)')
    args = p.parse_args(argv)

    today = _today()
    master = _load_master()
    n0 = len(master)
    print(f'earnings master: {n0} rows, '
          f'{master["ticker"].nunique() if n0 else 0} tickers, '
          f'max last_updated {master["last_updated"].max() if n0 else None}')

    # Headline numbers first: collector.js surfaces only the first 200 chars
    # of the last two stdout lines.
    counters = {
        'source': 'none', 'rows_new': 0, 'rows_updated': 0,
        'actuals_filled': 0, 'tickers_touched': 0,
        'master_rows_before': n0, 'master_rows_after': n0,
        'master_max_last_updated': None,
        'fmp_rows_fetched': 0, 'fmp_windows_ok': 0, 'fmp_windows_failed': 0,
        'fmp_rows_in_universe': 0, 'fmp_rows_dropped_not_in_universe': 0,
        'calendar_forward_rows': 0, 'yf_tickers_queried': 0, 'yf_rows': 0,
    }

    # 1. Forward merge from the LOCAL yfinance calendar (skipped in backfill).
    fwd = pd.DataFrame(columns=MASTER_COLUMNS)
    if not args.backfill:
        fwd = forward_rows(master, today)
        counters['calendar_forward_rows'] = len(fwd)
        print(f'  forward merge: +{len(fwd)} upcoming events from '
              f'earnings_calendar.parquet')

    # 2. PRIMARY: FMP /stable bulk sweep.
    fmp_ok = False
    source = 'none'
    if not args.backfill:
        start = today - timedelta(days=args.lookback_days)
        end = today + timedelta(days=args.horizon_days)
        try:
            raw, n_ok, n_fail = fetch_fmp_calendar(
                start, end, os.environ.get('FMP_API_KEY', ''), pace_s=args.pace)
            counters.update(fmp_rows_fetched=len(raw), fmp_windows_ok=n_ok,
                            fmp_windows_failed=n_fail)
            rows = fmp_rows_to_master(raw, today)
            allowed = (set(master['ticker'].dropna().astype(str))
                       | set(_active_universe())
                       | _universe_file_tickers(args.universe_file))
            rows, n_dropped = filter_universe(rows, allowed)
            counters.update(fmp_rows_in_universe=len(rows),
                            fmp_rows_dropped_not_in_universe=n_dropped)
            print(f'  fmp sweep {start}..{end}: {len(raw)} rows, '
                  f'{n_ok} windows ok / {n_fail} failed; '
                  f'{len(rows)} rows in universe ({n_dropped} dropped)')
            if rows.empty:
                raise FmpUnavailable('FMP returned no rows for the universe')
            master, stats = merge_rows(master, rows, today)
            fmp_ok = True
            source = 'fmp'
        except FmpUnavailable as e:
            print(f'  fmp PRIMARY unavailable: {e} — falling back to yfinance',
                  file=sys.stderr)
            stats = None

    # 3. FALLBACK (or --backfill): yfinance actuals for recently-reported names.
    fallback_ok = False
    if not fmp_ok:
        if args.backfill:
            if args.tickers:
                targets = sorted({t.strip() for t in args.tickers.split(',')
                                  if t.strip()})
            else:
                targets = sorted(pd.read_parquet(
                    PRICES_PATH, columns=['ticker'])['ticker'].unique())
            if args.limit:
                targets = targets[:args.limit]
        else:
            targets = tickers_needing_actuals(master, today)
        counters['yf_tickers_queried'] = len(targets)
        print(f'  yfinance actuals fetch: {len(targets)} tickers')
        actuals = _fetch_actuals(targets, args.throttle) if targets else None
        counters['yf_rows'] = 0 if actuals is None else len(actuals)
        master, stats = merge_rows(master, actuals, today)
        fallback_ok = actuals is not None and not actuals.empty
        source = 'yfinance' if fallback_ok else 'none'

    counters.update(rows_new=stats['rows_new'], rows_updated=stats['rows_updated'],
                    actuals_filled=stats['actuals_filled'],
                    tickers_touched=len(stats['tickers_touched']))

    if not fwd.empty:
        master = pd.concat([master, fwd], ignore_index=True)
        counters['rows_new'] += len(fwd)

    if len(master) < n0:
        print(f'  ABORT: merge would shrink master {n0} → {len(master)} rows '
              f'— append-only violated, not writing', file=sys.stderr)
        return 1

    both_failed = not fmp_ok and not fallback_ok
    if both_failed and fwd.empty:
        print('  nothing to write: FMP and yfinance both failed, no calendar rows',
              file=sys.stderr)
    else:
        _atomic_write(master, dry_run=args.dry_run)
        verb = 'would write' if args.dry_run else 'wrote'
        print(f'  {verb} {len(master)} rows ({len(master) - n0:+d}) → {MASTER_PATH}')

    counters['source'] = source
    counters['master_rows_before'] = n0
    counters['master_rows_after'] = len(master)
    counters['master_max_last_updated'] = (
        master['last_updated'].max() if len(master) else None)
    print('  counters: ' + ' '.join(f'{k}={v}' for k, v in counters.items()))

    if both_failed:
        print('  FAIL: FMP primary AND yfinance fallback both failed',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
