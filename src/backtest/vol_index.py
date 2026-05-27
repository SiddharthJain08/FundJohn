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
import pandas as pd
import pyarrow.parquet as pq

# real_IV ≈ BETA * (VIX/100). SPY/SPX structurally ≈ VIX; QQQ/IWM from overlap calibration.
OPTION_UNDERLYING_BETA: dict[str, float] = {
    'SPY': 0.90, 'SPX': 1.00, '^GSPC': 1.00, 'QQQ': 1.25, 'IWM': 1.36,
}
# Only these underlyings are trusted for option strategy origination/promotion in Phase 0.
VALID_OPTION_UNDERLYINGS = frozenset(OPTION_UNDERLYING_BETA)

_PRICES = 'data/master/prices.parquet'


@functools.lru_cache(maxsize=1)
def _vix_series() -> pd.Series:
    df = pq.read_table(_PRICES, columns=['ticker', 'date', 'close']).to_pandas()
    v = df[df['ticker'] == '^VIX'].copy()
    v['date'] = pd.to_datetime(v['date'])
    return (v.set_index('date')['close'].sort_index()) / 100.0  # decimal vol


def is_supported_option_underlying(underlying: str) -> bool:
    return underlying in VALID_OPTION_UNDERLYINGS


def vix_anchored_iv(underlying: str, as_of) -> float | None:
    """beta * VIX as of `as_of` for a supported underlying; None otherwise or if
    no VIX history up to as_of."""
    beta = OPTION_UNDERLYING_BETA.get(underlying)
    if beta is None:
        return None
    s = _vix_series()
    asof = pd.Timestamp(as_of)
    hist = s.loc[:asof]
    if len(hist) == 0:
        return None
    return float(beta) * float(hist.iloc[-1])
