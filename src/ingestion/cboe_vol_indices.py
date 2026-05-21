# src/ingestion/cboe_vol_indices.py
"""SP-1: SOLE allowed yfinance importer in the codebase.

CI lint (scripts/lint_provider_guards.py) enforces this — adding
`import yfinance` anywhere else fails the build.

Surface:
    get_vix()       — CBOE Volatility Index (^VIX)
    get_vvix()      — CBOE VVIX (vol-of-vol) (^VVIX)
    get_vix3m()     — 3-month VIX (^VIX3M)
    get_vix9d()     — 9-day VIX (^VIX9D)

Optional (gated by FMP forward-earnings probe outcome in Task 21):
    get_forward_earnings_calendar()  — only if FMP Starter doesn't cover it.
"""
from __future__ import annotations

import logging
import time

import pandas as pd
import yfinance as yf  # SOLE ALLOWED yfinance IMPORT (enforced by lint)

log = logging.getLogger(__name__)


def _yf_download(ticker: str, *, period: str = '1y', interval: str = '1d') -> pd.DataFrame:
    """Single seam for retry + monkeypatching in tests."""
    return yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)


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
    Returns DataFrame with columns: ticker, calendar (dict)."""
    rows = []
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar
            if cal is not None and not cal.empty:
                rows.append({'ticker': t, 'calendar': cal.to_dict()})
        except Exception as e:
            log.warning('yfinance forward earnings for %s: %s', t, e)
    return pd.DataFrame(rows)
