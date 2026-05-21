# src/ingestion/cboe_vol_indices.py
"""SP-1: SOLE allowed yfinance importer in the codebase for CBOE vol indices.

CI lint (scripts/lint_provider_guards.py) enforces this — adding
`import yfinance` anywhere else (except src/ingestion/yfinance_gateway.py)
fails the build.

Surface:
    get_vix()       — CBOE Volatility Index (^VIX)
    get_vvix()      — CBOE VVIX (vol-of-vol) (^VVIX)
    get_vix3m()     — 3-month VIX (^VIX3M)
    get_vix9d()     — 9-day VIX (^VIX9D)

    main()          — incremental fetch of all 4 indices → macro.parquet.
                      Returns {series_name: row_count} dict.
                      Called by:
                        - src/pipeline/backfillers/yfinance.py _backfill_vol_index
                        - __main__ block (used by src/pipeline/collector.js subprocess)

Optional (gated by FMP forward-earnings probe outcome in Task 21):
    get_forward_earnings_calendar()  — only if FMP Starter doesn't cover it.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf  # SOLE ALLOWED yfinance IMPORT (enforced by lint)

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent

# How many days back to backfill when no existing rows for a series.
BACKFILL_DAYS = 5 * 365

VOL_INDICES = [
    ('^VIX',   'VIX'),
    ('^VIX3M', 'VIX3M'),
    ('^VVIX',  'VVIX'),
    ('^VIX9D', 'VIX9D'),
]


def _yf_download(ticker: str, *, period: str | None = '1y', interval: str = '1d',
                 start: str | None = None, end: str | None = None,
                 auto_adjust: bool = False) -> pd.DataFrame:
    """Single seam for retry + monkeypatching in tests.

    Accepts either period= OR start=/end= kwargs (never both).
    auto_adjust=False by default (raw Close); pass True for adjusted prices.
    """
    kw: dict = {'interval': interval, 'progress': False, 'auto_adjust': auto_adjust}
    if start is not None:
        kw['start'] = start
        if end is not None:
            kw['end'] = end
    else:
        kw['period'] = period or '1y'
    return yf.download(ticker, **kw)


def _fetch_with_retry(ticker: str, *, retries: int = 1, **kw) -> pd.DataFrame:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            df = _yf_download(ticker, **kw)
            if df is None or df.empty:
                raise RuntimeError(f'{ticker} returned empty DataFrame')
            return df
        except Exception as e:
            last = e
            log.warning('yfinance fetch attempt %d/%d for %s failed: %s',
                        attempt + 1, retries + 1, ticker, e)
            time.sleep(1.0)
    raise last if last else RuntimeError(f'{ticker} fetch failed')


def get_vix(**kw) -> pd.DataFrame:
    return _fetch_with_retry('^VIX', **kw)


def get_vvix(**kw) -> pd.DataFrame:
    return _fetch_with_retry('^VVIX', **kw)


def get_vix3m(**kw) -> pd.DataFrame:
    return _fetch_with_retry('^VIX3M', **kw)


def get_vix9d(**kw) -> pd.DataFrame:
    return _fetch_with_retry('^VIX9D', **kw)


def get_forward_earnings_calendar(tickers: list[str]) -> pd.DataFrame:
    """Per-ticker forward earnings via yfinance Ticker(t).calendar.

    Only invoked if FMP per-ticker forward endpoint fails Starter probe (Task 21).
    Returns DataFrame with columns: ticker, calendar (dict).

    yfinance may return calendar as a dict (newer versions) or DataFrame
    (older versions). Both are normalised to a plain Python dict before
    storing so callers always get dict.get('Earnings Date') / .get('Earnings
    Average') / .get('Revenue Average') semantics."""
    rows = []
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar
            if cal is None:
                continue
            # Normalise: DataFrame → dict (older yfinance versions return
            # a DataFrame with a single-row or transposed layout).
            if isinstance(cal, pd.DataFrame):
                if cal.empty:
                    continue
                # Transposed form: index = field names, columns = dates/values.
                cal = cal.to_dict()
            elif not cal:  # empty dict / falsy
                continue
            rows.append({'ticker': t, 'calendar': cal})
        except Exception as e:
            log.warning('yfinance forward earnings for %s: %s', t, e)
    return pd.DataFrame(rows)


# ── Incremental macro.parquet writer (mirrors old fetch_vol_indices.main) ─────

def _fetch_series_for_macro(yf_ticker: str, start: str, end_inclusive: str) -> pd.DataFrame:
    """Fetch Close series for a vol index, returning DataFrame[date, value]."""
    end_excl = (date.fromisoformat(end_inclusive) + timedelta(days=1)).isoformat()
    df = _yf_download(yf_ticker, period=None, start=start, end=end_excl)
    if df is None or df.empty:
        return pd.DataFrame(columns=['date', 'value'])
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[['Close']].rename(columns={'Close': 'value'})
    df.index.name = 'date'
    df = df.reset_index()
    df['date'] = pd.to_datetime(df['date']).dt.date
    return df.dropna(subset=['value'])[['date', 'value']]


def _macro_max_date(series_name: str) -> str | None:
    """Return the latest date string for a series in macro.parquet, or None."""
    macro_path = ROOT / 'data' / 'master' / 'macro.parquet'
    if not macro_path.exists():
        return None
    try:
        df = pd.read_parquet(macro_path, columns=['date', 'series'])
        sub = df[df['series'] == series_name]
        if sub.empty:
            return None
        return pd.to_datetime(sub['date']).max().date().isoformat()
    except Exception:
        return None


def main() -> dict[str, int]:
    """Incremental fetch of all 4 vol-index series into macro.parquet.

    Returns {series_name: row_count_fetched}.
    Called by backfillers/yfinance.py _backfill_vol_index and by __main__.
    """
    sys.path.insert(0, str(ROOT))
    from src.data import parquet_store as ps  # noqa: E402 (lazy to keep module importable standalone)

    today = date.today().isoformat()
    results: dict[str, int] = {}
    all_rows: list[dict] = []

    for yf_ticker, series_name in VOL_INDICES:
        try:
            max_date = _macro_max_date(series_name)
            if max_date:
                start = (date.fromisoformat(max_date) + timedelta(days=1)).isoformat()
            else:
                start = (date.today() - timedelta(days=BACKFILL_DAYS)).isoformat()

            if start >= today:
                results[series_name] = 0
                continue

            df = _fetch_series_for_macro(yf_ticker, start, today)
            if df.empty:
                results[series_name] = 0
                continue

            for _, row in df.iterrows():
                d = row['date']
                if isinstance(d, str):
                    d = date.fromisoformat(d)
                all_rows.append({
                    'date':   d,
                    'series': series_name,
                    'value':  float(row['value']),
                    'source': 'yfinance',
                })
            results[series_name] = len(df)
            log.info('%s: +%d rows (%s -> %s)', series_name, len(df), start, today)

        except Exception as e:
            log.error('%s: ERROR - %s', series_name, e)
            results[series_name] = -1

    if all_rows:
        total = ps.write_macro(all_rows)
        log.info('macro.parquet total rows after write: %d', total)

    return results


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='  %(message)s')
    out = main()
    print(json.dumps({'status': 'ok', 'inserted': out}))
