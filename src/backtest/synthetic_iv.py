"""Synthetic implied-vol model for the options backtest engine (SP-4 Phase 0;
spec 2026-09-06 B.3 anchor hierarchy).

synthetic_iv_detail resolves, in order:
  1. 'surface'  — the REAL surface master (options_surface.parquet: iv30/iv90 as-of
                  the underlying, ≤ 7 days old), constant-maturity to `dte` in total
                  variance (flat iv30 below 30 d, flat iv90 above 90 d);
  2. 'vix_term' — beta × the VIX9D/VIX term point for OPTION_UNDERLYING_BETA names;
  3. 'realized' — trailing realized vol × VRP factor, floored.
The VRP factor (and window) are CALIBRATED by scripts/options_parity_check.py.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

import numpy as np
import pandas as pd

IV_FLOOR = 0.05
DEFAULT_WINDOW = 21          # trading days
DEFAULT_VRP_FACTOR = 1.15    # placeholder; calibrated by options_parity_check.py
TRADING_DAYS = 252
ROOT = Path(__file__).resolve().parents[2]
SURFACE_PATH_ENV = 'OPENCLAW_OPTIONS_SURFACE_PATH'
SURFACE_ASOF_TOLERANCE = pd.Timedelta(days=7)
SURFACE_DTES = (30, 90)


def realized_vol(prices: pd.Series, window: int = DEFAULT_WINDOW) -> float:
    """Annualized close-to-close realized vol over the trailing `window` days."""
    s = prices.dropna()
    if len(s) < 3:
        return IV_FLOOR
    rets = s.pct_change().dropna().iloc[-window:]
    if len(rets) < 2:
        return IV_FLOOR
    return float(rets.std(ddof=1) * np.sqrt(TRADING_DAYS))


def surface_path() -> Path:
    return Path(os.environ.get(SURFACE_PATH_ENV) or (ROOT / 'data' / 'master' / 'options_surface.parquet'))


def clear_cache() -> None:
    _surface_rows.cache_clear()


@functools.lru_cache(maxsize=512)
def _surface_rows(ticker: str, path_str: str, mtime_ns: int) -> pd.DataFrame:
    import pyarrow.parquet as pq
    df = pq.read_table(path_str, columns=['date', 'iv30', 'iv90'],
                       filters=[('ticker', '==', ticker)]).to_pandas()
    df['date'] = pd.to_datetime(df['date']).dt.normalize()
    df['iv30'] = pd.to_numeric(df['iv30'], errors='coerce')
    df['iv90'] = pd.to_numeric(df['iv90'], errors='coerce')
    return (df.dropna(subset=['iv30']).sort_values('date')
              .drop_duplicates('date', keep='last').reset_index(drop=True))


def surface_iv(underlying: str, as_of, dte: int = 30) -> float | None:
    """Tier 1: the real surface's constant-maturity ATM IV at `dte`, or None."""
    p = surface_path()
    try:
        if not p.exists():
            return None
        rows = _surface_rows(str(underlying), str(p), p.stat().st_mtime_ns)
    except Exception:  # noqa: BLE001 — an unreadable master degrades to the next tier
        return None
    if rows.empty:
        return None
    asof = pd.Timestamp(as_of).normalize()
    prior = rows[rows['date'] <= asof]
    if prior.empty or (asof - prior['date'].iloc[-1]) > SURFACE_ASOF_TOLERANCE:
        return None
    r = prior.iloc[-1]
    iv30 = float(r['iv30'])
    if not (iv30 > 0):
        return None
    iv90 = float(r['iv90']) if r['iv90'] == r['iv90'] else None
    if iv90 is None or not (iv90 > 0):
        return iv30
    from backtest.vol_index import interp_total_variance
    return interp_total_variance(SURFACE_DTES[0], iv30, SURFACE_DTES[1], iv90, int(dte))


def synthetic_iv_detail(prices: pd.Series, vrp_factor: float = DEFAULT_VRP_FACTOR,
                        window: int = DEFAULT_WINDOW, underlying: str | None = None,
                        as_of=None, dte: int = 30) -> tuple[float, str]:
    """(iv, source) for an underlying as of the last bar in `prices`."""
    if underlying is not None and as_of is not None:
        s = surface_iv(underlying, as_of, dte)
        if s is not None:
            return max(IV_FLOOR, float(s)), 'surface'
        from backtest.vol_index import vix_anchored_iv
        anchored = vix_anchored_iv(underlying, as_of, dte)
        if anchored is not None:
            return max(IV_FLOOR, float(anchored)), 'vix_term'
    rv = realized_vol(prices, window=window)
    return max(IV_FLOOR, float(rv) * float(vrp_factor)), 'realized'


def synthetic_iv(prices: pd.Series, vrp_factor: float = DEFAULT_VRP_FACTOR,
                 window: int = DEFAULT_WINDOW, underlying: str | None = None,
                 as_of=None, dte: int = 30) -> float:
    """Modeled IV — see synthetic_iv_detail for the tier order."""
    return synthetic_iv_detail(prices, vrp_factor=vrp_factor, window=window,
                               underlying=underlying, as_of=as_of, dte=dte)[0]
