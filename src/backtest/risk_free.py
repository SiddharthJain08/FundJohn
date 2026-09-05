"""Risk-free rate for every Sharpe and pricing site (spec 2026-09-04 Part C).

Two sources, selected by OPENCLAW_RF_SOURCE:
  const  — 5 % flat (the pre-2026-09-04 behaviour at all six sites)     [default]
  macro  — FRED DGS3MO from data/master/macro.parquet, per date, forward-filled

excess_sharpe(r, dates) = mean(r_t − rf_t) / std(r, ddof=1) · √252 — the
standard deviation of the RAW returns, so 'const' reproduces the legacy
formula (mean(r) − rf)/std(r) bit-for-bit.
"""
from __future__ import annotations

import functools
import logging
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
RISK_FREE_ANNUAL_CONST = 0.05
TRADING_DAYS = 252
RF_SERIES = 'DGS3MO'
MACRO_PATH_ENV = 'OPENCLAW_MACRO_PARQUET'
SOURCE_ENV = 'OPENCLAW_RF_SOURCE'
_WARNED = False


def macro_path() -> Path:
    return Path(os.environ.get(MACRO_PATH_ENV) or (ROOT / 'data' / 'master' / 'macro.parquet'))


def rf_source() -> str:
    s = (os.environ.get(SOURCE_ENV) or 'const').strip().lower()
    return s if s in ('const', 'macro') else 'const'


def clear_cache() -> None:
    _load.cache_clear()
    global _WARNED
    _WARNED = False


@functools.lru_cache(maxsize=2)
def _load(path_str: str, mtime_ns: int) -> pd.Series:
    import pyarrow.parquet as pq
    tbl = pq.read_table(path_str, columns=['date', 'series', 'value'],
                        filters=[('series', '==', RF_SERIES)])
    df = tbl.to_pandas()
    df['date'] = pd.to_datetime(df['date'])
    s = df.dropna(subset=['value']).set_index('date')['value'].sort_index()
    s = s[~s.index.duplicated(keep='last')].astype(float) / 100.0
    return s


def _series() -> pd.Series | None:
    global _WARNED
    p = macro_path()
    try:
        if p.exists():
            s = _load(str(p), p.stat().st_mtime_ns)
            if len(s):
                return s
    except Exception as exc:  # noqa: BLE001
        log.warning('risk_free: %s unreadable (%s)', p, exc)
    if not _WARNED:
        log.warning('risk_free: %s series unavailable at %s — falling back to constant %.2f%%',
                    RF_SERIES, p, RISK_FREE_ANNUAL_CONST * 100)
        _WARNED = True
    return None


def rf_annual_asof(d, source: str | None = None) -> float:
    src = source or rf_source()
    if src == 'const':
        return RISK_FREE_ANNUAL_CONST
    s = _series()
    if s is None:
        return RISK_FREE_ANNUAL_CONST
    ts = pd.Timestamp(d).normalize()
    if ts < s.index[0]:
        return float(s.iloc[0])
    v = s.asof(ts)
    return float(v) if v == v else float(s.iloc[-1])


def rf_daily_for(dates, source: str | None = None) -> np.ndarray:
    src = source or rf_source()
    idx = pd.DatetimeIndex(pd.to_datetime(list(dates))).normalize()
    if src == 'const' or _series() is None:
        return np.full(len(idx), RISK_FREE_ANNUAL_CONST / TRADING_DAYS)
    s = _series()
    aligned = s.reindex(s.index.union(idx)).sort_index().ffill().reindex(idx)
    aligned = aligned.fillna(float(s.iloc[0]))
    return aligned.to_numpy(dtype=float) / TRADING_DAYS


def _rf_vector(n: int, dates, source: str | None, asof) -> np.ndarray:
    if dates is not None:
        v = rf_daily_for(dates, source)
        if len(v) != n:
            raise ValueError(f'risk_free: {len(v)} dates for {n} returns')
        return v
    return np.full(n, rf_annual_asof(asof or pd.Timestamp.today(), source) / TRADING_DAYS)


def excess_sharpe(rets, dates=None, source: str | None = None, min_obs: int = 2, asof=None) -> float | None:
    r = np.asarray(list(rets), dtype=float)
    n = len(r)
    if n < max(int(min_obs), 2):
        return None
    sd = float(r.std(ddof=1))
    if not math.isfinite(sd) or sd < 1e-9:
        return None
    rfv = _rf_vector(n, dates, source, asof)
    return float((r - rfv).mean() / sd * math.sqrt(TRADING_DAYS))


def sharpe_pair(rets, dates=None, asof=None) -> dict:
    r = np.asarray(list(rets), dtype=float)
    n = len(r)
    macro_v = _rf_vector(n, dates, 'macro', asof) if n else np.array([])
    return {
        'const': excess_sharpe(r, dates, 'const', asof=asof),
        'macro': excess_sharpe(r, dates, 'macro', asof=asof),
        'rf_mean_annual': float(macro_v.mean() * TRADING_DAYS) if n else None,
        'n': int(n),
    }


def _fmt(v) -> str:
    return 'n/a' if v is None else f'{v:.3f}'


def shadow_line(site: str, rets, dates=None, asof=None) -> str:
    p = sharpe_pair(rets, dates, asof)
    return (f"[rf_shadow] site={site} const={_fmt(p['const'])} macro={_fmt(p['macro'])} "
            f"n={p['n']} rf_mean={_fmt(p['rf_mean_annual'])}")
