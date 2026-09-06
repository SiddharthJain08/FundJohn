"""Vol-index anchoring for synthetic option IV (SP-4 Phase 0.5).

For liquid index/ETF underlyings, anchor ATM implied vol to ^VIX (10y of REAL,
regime-diverse 30-day SPX implied vol in prices.parquet) scaled by a per-name
beta, instead of trailing realized vol. VIX *is* implied vol, so this is sound
across regimes — unlike a realized-vol proxy. Betas were calibrated against the
real option chain (options_eod.parquet) over the 2026-05-22..26 overlap and
sanity-checked structurally (SPY≈SPX≈VIX). Single names / underlyings with no
beta fall back to the realized-vol model (lower fidelity) and are NOT in the
trusted option-origination scope.
"""
from __future__ import annotations
import functools
import math
import os
from pathlib import Path
import pandas as pd
import pyarrow.parquet as pq

# real_IV ≈ BETA * (VIX/100). SPY/SPX structurally ≈ VIX; QQQ/IWM from overlap calibration.
OPTION_UNDERLYING_BETA: dict[str, float] = {
    'SPY': 0.90, 'SPX': 1.00, '^GSPC': 1.00, 'QQQ': 1.25, 'IWM': 1.36,
}
# Only these underlyings are trusted for option strategy origination/promotion in Phase 0.
VALID_OPTION_UNDERLYINGS = frozenset(OPTION_UNDERLYING_BETA)

_PRICES = 'data/master/prices.parquet'

_VOL_INDICES_ENV = 'OPENCLAW_VOL_INDICES_PARQUET'
_VOL_INDICES = 'data/master/vol_indices.parquet'
VIX9D_DTE, VIX_DTE = 9, 30


@functools.lru_cache(maxsize=1)
def _vix_series() -> pd.Series:
    df = pq.read_table(_PRICES, columns=['ticker', 'date', 'close']).to_pandas()
    v = df[df['ticker'] == '^VIX'].copy()
    v['date'] = pd.to_datetime(v['date'])
    return (v.set_index('date')['close'].sort_index()) / 100.0  # decimal vol


@functools.lru_cache(maxsize=1)
def _vix9d_series() -> pd.Series:
    """VIX9D closes (decimal vol) from vol_indices.parquet; EMPTY when the file
    or column is unavailable (the term point then degrades to flat VIX)."""
    p = Path(os.environ.get(_VOL_INDICES_ENV) or _VOL_INDICES)
    try:
        df = pq.read_table(p, columns=['date', 'vix9d_close']).to_pandas()
    except Exception:  # noqa: BLE001
        return pd.Series(dtype=float)
    df['date'] = pd.to_datetime(df['date'])
    s = df.dropna(subset=['vix9d_close']).set_index('date')['vix9d_close'].sort_index() / 100.0
    return s[~s.index.duplicated(keep='last')].astype(float)


def interp_total_variance(d1: float, v1: float, d2: float, v2: float, target: float) -> float:
    """Linear in total variance σ²·T between (d1, v1) and (d2, v2), days; flat outside."""
    if target <= d1:
        return float(v1)
    if target >= d2:
        return float(v2)
    t1, t2, tt = d1 / 365.0, d2 / 365.0, target / 365.0
    w = v1 * v1 * t1 + (v2 * v2 * t2 - v1 * v1 * t1) * (tt - t1) / (t2 - t1)
    return math.sqrt(max(w, 0.0) / tt)


def vix_term_point(as_of, dte: int = 30) -> float | None:
    """VIX term point at `dte` (decimal vol): VIX9D (9 d) ↔ VIX (30 d) interpolated
    in total variance, flat VIX9D below 9 d, flat VIX above 30 d (spec 2026-09-06 B.3;
    no VIX3M in any master). At dte = 30 this is exactly the VIX close, so the
    legacy `beta × VIX` contract holds."""
    hist = _vix_series().loc[:pd.Timestamp(as_of)]
    if len(hist) == 0:
        return None
    v30 = float(hist.iloc[-1])
    if dte >= VIX_DTE:
        return v30
    s9 = _vix9d_series()
    h9 = s9.loc[:pd.Timestamp(as_of)] if len(s9) else s9
    if len(h9) == 0:
        return v30
    return interp_total_variance(VIX9D_DTE, float(h9.iloc[-1]), VIX_DTE, v30, dte)


def is_supported_option_underlying(underlying: str) -> bool:
    return underlying in VALID_OPTION_UNDERLYINGS


def vix_anchored_iv(underlying: str, as_of, dte: int = 30) -> float | None:
    """beta × the VIX term point at `dte` for a supported underlying; None otherwise
    or if no VIX history up to as_of."""
    beta = OPTION_UNDERLYING_BETA.get(underlying)
    if beta is None:
        return None
    pt = vix_term_point(as_of, dte)
    return None if pt is None else float(beta) * pt
