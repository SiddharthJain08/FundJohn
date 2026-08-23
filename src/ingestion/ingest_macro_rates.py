"""Free macro/rates stream → data/master/macro.parquet (2026-08-23).

Until today macro.parquet held ONLY the VIX family (VIX/VIX3M/VIX9D/VVIX,
source=yfinance, written by ingest_vol_indices.py / cboe_vol_indices.py). Three
strategies were blocked on rates/macro series that were never ingested:
S_ast_fed_model hardcodes a 2% risk-free ("FRED DGS3MO unavailable"),
S_macro_risk_momentum_ip_beta looks for `INDPRO`, and the regime-timing
strategies only see vol. This module adds two KEYLESS sources:

  1. FRED keyless CSV  — `GET https://fred.stlouisfed.org/graph/fredgraph.csv?id=<ID>`
     returns the FULL history of one series as `observation_date,<ID>` with `.`
     for missing values. One call per series in `FRED_SERIES`, 1 s apart, with
     a polite User-Agent. Series name == FRED id, source='fred'.
  2. NY Fed reference rates (same-day; FRED lags a day) —
     `GET https://markets.newyorkfed.org/api/rates/all/search.json?startDate=…&endDate=…&type=rate`
     → {"refRates": [{"effectiveDate", "type", "percentRate", …}, …]}. Only the
     `percentRate` types SOFR/EFFR/OBFR/TGCR/BGCR are ingested, as series
     `NYFED_<TYPE>` (SOFRAI averages/index rows are skipped), source='nyfed'.

Modes
-----
  default (incremental): FRED rows with date ≥ (master max date for that series
      − FRED_OVERLAP_DAYS); a series absent from the master takes its full
      history. NY Fed: last NYFED_LOOKBACK_DAYS days.
  --full: entire history for everything (first run). FRED's CSV is always the
      full history — the window is applied client-side (memory is trivial:
      ~17k rows per series).

Write path: `parquet_store.append_dedup(MACRO_PATH, df, MACRO_KEYS, mode='replace')`
— exactly what `parquet_store.write_macro` does — so the existing schema
(date32 / string / double / string) is preserved and the VIX rows are never
touched (masters are APPEND-ONLY — CLAUDE.md core invariant). Rows are built
with `datetime.date` objects so the `date` column arrives as date32, matching
how cboe_vol_indices builds its rows.

Counters are the contract ("read the COUNTERS, not the exit code"):
  rc=1  no source could be read at all (every FRED series AND NY Fed failed)
  rc=0  otherwise; series_ok / series_failed / rows_fetched / rows_submitted /
        rows_written (= master row delta after dedup) / per-series max date
        are printed to stdout; per-series failures are counted, never fatal.

Usage
-----
    python3 src/ingestion/ingest_macro_rates.py              # incremental
    python3 src/ingestion/ingest_macro_rates.py --full       # first run / rebuild window
    python3 src/ingestion/ingest_macro_rates.py --series DGS3MO,DGS10
    python3 src/ingestion/ingest_macro_rates.py --dry-run    # fetch + count, no write
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # invoked as `python3 src/ingestion/…` by systemd
    sys.path.insert(0, str(ROOT))

from src.data.parquet_store import MACRO_KEYS, MACRO_PATH, append_dedup, row_count  # noqa: E402

# ── Sources ───────────────────────────────────────────────────────────────────

FRED_CSV_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv'
NYFED_SEARCH_URL = 'https://markets.newyorkfed.org/api/rates/all/search.json'
USER_AGENT = 'openclaw-macro-rates/1.0 (research data ingest; python-requests)'

# Operator-extendable. Series name in macro.parquet == FRED series id.
FRED_SERIES: tuple[str, ...] = (
    # Treasury constant-maturity curve
    'DGS1MO', 'DGS3MO', 'DGS6MO', 'DGS1', 'DGS2', 'DGS5', 'DGS10', 'DGS30',
    'DTB3',                      # 3-month T-bill secondary market
    'T10Y2Y', 'T10Y3M',          # curve spreads
    'DFF', 'SOFR',               # policy / funding (FRED copies; NY Fed below is same-day)
    'BAMLH0A0HYM2', 'BAMLC0A0CM',  # HY OAS / IG OAS
    'DTWEXBGS',                  # broad USD index
    'DCOILWTICO',                # WTI
    'DEXUSEU', 'DEXJPUS',        # FX
    'INDPRO',                    # monthly industrial production — S_macro_risk_momentum_ip_beta reads it
)

# NY Fed `type` values carrying `percentRate`; stored as NYFED_<TYPE>.
NYFED_TYPES: tuple[str, ...] = ('SOFR', 'EFFR', 'OBFR', 'TGCR', 'BGCR')
NYFED_PREFIX = 'NYFED_'
NYFED_FULL_START = '2014-01-01'   # EFFR starts 2014-01-02 in the API; SOFR/TGCR/BGCR 2018-04-02

FRED_OVERLAP_DAYS = 7
NYFED_LOOKBACK_DAYS = 14
SLEEP_BETWEEN_CALLS_S = 1.0
HTTP_TIMEOUT_S = 60
FRED_MISSING = '.'


class MacroSourceError(RuntimeError):
    """A source answered with something that is not the data we asked for."""


# ── Parsing (pure) ────────────────────────────────────────────────────────────

def parse_fred_csv(text: str, series: str) -> list[dict]:
    """`observation_date,<ID>` CSV → rows {date, series, value, source='fred'}.
    Drops FRED's `.` missing marker. Raises MacroSourceError when the body is
    not the expected CSV (empty / HTML block page) so the caller counts a
    FAILURE rather than a silent zero-row success."""
    if not text or not text.lstrip().lower().startswith('observation_date'):
        raise MacroSourceError(f'{series}: response is not a FRED CSV '
                               f'(starts with {text[:40]!r})')
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    if len(header) < 2:
        raise MacroSourceError(f'{series}: malformed FRED header {header!r}')
    rows: list[dict] = []
    for rec in reader:
        if len(rec) < 2:
            continue
        raw = rec[1].strip()
        if raw == FRED_MISSING or raw == '':
            continue
        rows.append({
            'date': date.fromisoformat(rec[0].strip()),
            'series': series,
            'value': float(raw),
            'source': 'fred',
        })
    return rows


def parse_nyfed_rates(payload: dict) -> list[dict]:
    """NY Fed `refRates` → rows for the percentRate types only (SOFRAI skipped)."""
    rows: list[dict] = []
    for rec in (payload or {}).get('refRates', []) or []:
        typ = rec.get('type')
        if typ not in NYFED_TYPES:
            continue
        rate = rec.get('percentRate')
        eff = rec.get('effectiveDate')
        if rate is None or not eff:
            continue
        rows.append({
            'date': date.fromisoformat(str(eff)[:10]),
            'series': NYFED_PREFIX + typ,
            'value': float(rate),
            'source': 'nyfed',
        })
    return rows


# ── Incremental window ────────────────────────────────────────────────────────

def master_max_dates(master_path: Path | str) -> dict[str, date]:
    """{series: max date} from the master (two columns only — trivial read)."""
    master_path = Path(master_path)
    if not master_path.exists():
        return {}
    df = pd.read_parquet(master_path, columns=['date', 'series'])
    if df.empty:
        return {}
    mx = pd.to_datetime(df['date']).groupby(df['series']).max()
    return {str(k): v.date() for k, v in mx.items()}


def fred_window_start(series: str, max_dates: dict[str, date],
                      overlap_days: int = FRED_OVERLAP_DAYS) -> date | None:
    """Incremental lower bound for a FRED series; None = series absent from the
    master → take its full history."""
    last = max_dates.get(series)
    if last is None:
        return None
    return last - timedelta(days=overlap_days)


def filter_since(rows: list[dict], since: date | None) -> list[dict]:
    if since is None:
        return rows
    return [r for r in rows if r['date'] >= since]


# ── HTTP (network — stubbed in tests) ─────────────────────────────────────────

def _session():
    import requests
    s = requests.Session()
    # Deliberately NOT a browser-like or custom UA. Measured 2026-08-23 from
    # this box: fred.stlouisfed.org (Akamai) answers `python-requests/*` and
    # `curl/*` in ~0.1 s but tar-pits 'Mozilla/5.0 …' and
    # 'openclaw-macro-rates/1.0 (…)' until the read timeout. Keep requests'
    # default User-Agent for FRED; markets.newyorkfed.org accepts either.
    s.headers.update({'Accept': 'text/csv, application/json, */*'})
    return s


def fetch_fred_csv(series: str, *, session=None, timeout: float = HTTP_TIMEOUT_S) -> str:
    s = session or _session()
    r = s.get(FRED_CSV_URL, params={'id': series}, timeout=timeout)
    r.raise_for_status()
    return r.text


def fetch_nyfed_rates(start: date, end: date, *, session=None,
                      timeout: float = HTTP_TIMEOUT_S) -> dict:
    s = session or _session()
    r = s.get(NYFED_SEARCH_URL,
              params={'startDate': start.isoformat(), 'endDate': end.isoformat(), 'type': 'rate'},
              timeout=timeout)
    r.raise_for_status()
    return r.json()


# ── Write ─────────────────────────────────────────────────────────────────────

def write_rows(rows: Iterable[dict], *, master_path: Path | str = MACRO_PATH) -> tuple[int, int]:
    """append_dedup on (date, series), mode='replace' — identical to
    parquet_store.write_macro but path-parameterised. Returns (before, after)."""
    master_path = Path(master_path)
    before = row_count(master_path)
    df = pd.DataFrame(list(rows), columns=['date', 'series', 'value', 'source'])
    if df.empty:
        return before, before
    df['value'] = df['value'].astype('float64')
    after = append_dedup(master_path, df, MACRO_KEYS, mode='replace')
    return before, int(after)


# ── Orchestration ─────────────────────────────────────────────────────────────

def run(*, series: Iterable[str] = FRED_SERIES, full: bool = False,
        master_path: Path | str = MACRO_PATH, today: date | None = None,
        fetch_fred: Callable[..., str] = None, fetch_nyfed: Callable[..., dict] = None,
        sleep_s: float | None = None, dry_run: bool = False) -> dict:
    """Fetch every source, count per-series outcomes, write once. Never raises
    on a per-source failure; rc=1 only when nothing at all could be read."""
    fetch_fred = fetch_fred or fetch_fred_csv
    fetch_nyfed = fetch_nyfed or fetch_nyfed_rates
    sleep_s = SLEEP_BETWEEN_CALLS_S if sleep_s is None else sleep_s
    today = today or date.today()
    master_path = Path(master_path)
    series = [s.strip() for s in series if s and s.strip()]

    stats: dict = {
        'mode': 'full' if full else 'incremental',
        'series_ok': 0, 'series_failed': 0, 'failed': {},
        'rows_fetched': 0, 'rows_submitted': 0, 'rows_written': 0,
        'master_rows_before': None, 'master_rows_after': None,
        'window_start': {}, 'max_date': {}, 'rc': 0,
    }
    max_dates = {} if full else master_max_dates(master_path)
    pending: list[dict] = []

    def _record(rows: list[dict], since: date | None, name: str) -> None:
        kept = filter_since(rows, since)
        stats['rows_fetched'] += len(rows)
        stats['rows_submitted'] += len(kept)
        stats['window_start'][name] = since
        if rows:
            stats['max_date'][name] = max(r['date'] for r in rows)
        pending.extend(kept)

    # FRED — one call per series, paced.
    for i, sid in enumerate(series):
        if i and sleep_s:
            time.sleep(sleep_s)
        try:
            rows = parse_fred_csv(fetch_fred(sid), sid)
        except Exception as exc:  # noqa: BLE001 — per-series failure is non-fatal by contract
            stats['series_failed'] += 1
            stats['failed'][sid] = f'{type(exc).__name__}: {exc}'
            print(f'[macro-rates] FRED {sid} FAILED: {type(exc).__name__}: {exc}', file=sys.stderr)
            continue
        stats['series_ok'] += 1
        _record(rows, None if full else fred_window_start(sid, max_dates), sid)

    # NY Fed — one call covers all types.
    ny_start = date.fromisoformat(NYFED_FULL_START) if full else today - timedelta(days=NYFED_LOOKBACK_DAYS)
    try:
        if series and sleep_s:
            time.sleep(sleep_s)
        ny_rows = parse_nyfed_rates(fetch_nyfed(ny_start, today))
    except Exception as exc:  # noqa: BLE001
        for typ in NYFED_TYPES:
            stats['series_failed'] += 1
            stats['failed'][NYFED_PREFIX + typ] = f'{type(exc).__name__}: {exc}'
        print(f'[macro-rates] NYFED FAILED: {type(exc).__name__}: {exc}', file=sys.stderr)
    else:
        by_name: dict[str, list[dict]] = {}
        for r in ny_rows:
            by_name.setdefault(r['series'], []).append(r)
        for typ in NYFED_TYPES:
            name = NYFED_PREFIX + typ
            stats['series_ok'] += 1
            _record(by_name.get(name, []), ny_start, name)

    if stats['series_ok'] == 0:
        stats['rc'] = 1
        return stats

    if dry_run:
        stats['master_rows_before'] = stats['master_rows_after'] = row_count(master_path)
        return stats

    before, after = write_rows(pending, master_path=master_path)
    stats['master_rows_before'], stats['master_rows_after'] = before, after
    stats['rows_written'] = after - before
    return stats


def _print_report(stats: dict) -> None:
    print(f"[macro-rates] mode={stats['mode']} series_ok={stats['series_ok']} "
          f"series_failed={stats['series_failed']} rows_fetched={stats['rows_fetched']} "
          f"rows_submitted={stats['rows_submitted']} rows_written={stats['rows_written']} "
          f"master_rows_before={stats['master_rows_before']} "
          f"master_rows_after={stats['master_rows_after']} rc={stats['rc']}")
    if stats['max_date']:
        print('[macro-rates] max_date ' + ' '.join(
            f'{k}={v.isoformat()}' for k, v in sorted(stats['max_date'].items())))
    if stats['failed']:
        print('[macro-rates] failed ' + ' | '.join(f'{k}: {v}' for k, v in sorted(stats['failed'].items())))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--full', action='store_true',
                    help='entire history for every series (first run); default = incremental')
    ap.add_argument('--series', default=None,
                    help='comma-separated FRED ids overriding FRED_SERIES (NY Fed always runs)')
    ap.add_argument('--dry-run', action='store_true', help='fetch + count, do not write')
    args = ap.parse_args(argv)

    series = args.series.split(',') if args.series else list(FRED_SERIES)
    t0 = time.monotonic()
    stats = run(series=series, full=args.full, master_path=MACRO_PATH,
                fetch_fred=fetch_fred_csv, fetch_nyfed=fetch_nyfed_rates,
                sleep_s=SLEEP_BETWEEN_CALLS_S, dry_run=args.dry_run)
    _print_report(stats)
    print(f'[macro-rates] elapsed={time.monotonic() - t0:.1f}s'
          + (' (dry-run: nothing written)' if args.dry_run else ''))
    if stats['rc']:
        print('[macro-rates] FAILED: no source could be read', file=sys.stderr)
    return stats['rc']


if __name__ == '__main__':
    sys.exit(main())
