"""Dividend yield `q` for the synthetic options engine (spec 2026-09-06 B.1).

q(ticker, as_of, spot) = Σ cash dividends with ex_date in (as_of − 365 d, as_of] / spot,
read from data/master/corporate_actions.parquet (action_type == 'cash_dividend').
Coverage starts 2024-02-09 in production: for as_of earlier than
coverage_start + 365 d the trailing window is incomplete, so q is BACKFILLED
with the ticker's first full trailing year (ruling G6) — divided by `ref_spot`
(the close at the backfill reference date) when the caller has it, else by
`spot` — and the module warns once per ticker. Never raises: a missing or
unreadable file, an unknown ticker or a non-positive spot ⇒ 0.0.
"""
from __future__ import annotations

import functools
import logging
import os
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
PATH_ENV = 'OPENCLAW_CORPORATE_ACTIONS_PARQUET'
TRAILING = pd.Timedelta(days=365)
_BACKFILL_WARNED: set[str] = set()
_MISSING_WARNED: set[str] = set()


def corporate_actions_path() -> Path:
    return Path(os.environ.get(PATH_ENV) or (ROOT / 'data' / 'master' / 'corporate_actions.parquet'))


def clear_cache() -> None:
    _load.cache_clear()
    _BACKFILL_WARNED.clear()
    _MISSING_WARNED.clear()


@functools.lru_cache(maxsize=2)
def _load(path_str: str, mtime_ns: int) -> pd.DataFrame:
    import pyarrow.parquet as pq
    tbl = pq.read_table(path_str, columns=['symbol', 'action_type', 'ex_date', 'cash_amount'],
                        filters=[('action_type', '==', 'cash_dividend')])
    df = tbl.to_pandas()
    df['symbol'] = df['symbol'].astype(str)
    df['ex_date'] = pd.to_datetime(df['ex_date'], errors='coerce').dt.normalize()
    df['cash_amount'] = pd.to_numeric(df['cash_amount'], errors='coerce')
    df = df.dropna(subset=['ex_date', 'cash_amount'])
    df = df[df['cash_amount'] > 0]
    return df[['symbol', 'ex_date', 'cash_amount']].sort_values(['symbol', 'ex_date']).reset_index(drop=True)


def _dividends() -> pd.DataFrame | None:
    p = corporate_actions_path()
    try:
        if p.exists():
            df = _load(str(p), p.stat().st_mtime_ns)
            return df if len(df) else None
    except Exception as exc:  # noqa: BLE001
        if str(p) not in _MISSING_WARNED:
            _MISSING_WARNED.add(str(p))
            log.warning('dividends: %s unreadable (%s) — q=0 everywhere', p, exc)
        return None
    if str(p) not in _MISSING_WARNED:
        _MISSING_WARNED.add(str(p))
        log.warning('dividends: %s missing — q=0 everywhere', p)
    return None


def coverage_start() -> pd.Timestamp | None:
    df = _dividends()
    return None if df is None else pd.Timestamp(df['ex_date'].min())


def backfill_reference_date() -> pd.Timestamp | None:
    cs = coverage_start()
    return None if cs is None else cs + TRAILING


def dividend_yield_asof(ticker: str, as_of, spot: float, ref_spot: float | None = None) -> float:
    df = _dividends()
    if df is None or spot is None or not (spot > 0):
        return 0.0
    d = df[df['symbol'] == str(ticker)]
    if d.empty:
        return 0.0
    as_of_ts = pd.Timestamp(as_of).normalize()
    lo = as_of_ts - TRAILING
    cs = pd.Timestamp(df['ex_date'].min())
    if lo < cs:
        # Incomplete trailing window (ruling G6): first full trailing year [cs, cs + 365 d).
        win = d[(d['ex_date'] >= cs) & (d['ex_date'] < cs + TRAILING)]
        if str(ticker) not in _BACKFILL_WARNED:
            _BACKFILL_WARNED.add(str(ticker))
            log.warning('dividends: q backfilled with the first full trailing year %s..%s for %s '
                        '(as_of %s precedes coverage + 365 d)', cs.date(), (cs + TRAILING).date(),
                        ticker, as_of_ts.date())
        denom = float(ref_spot) if (ref_spot is not None and ref_spot > 0) else float(spot)
        return float(win['cash_amount'].sum() / denom)
    win = d[(d['ex_date'] > lo) & (d['ex_date'] <= as_of_ts)]
    return float(win['cash_amount'].sum() / float(spot))
