"""Synthetic implied-vol model for the options backtest engine (SP-4 Phase 0).

IV(underlying, as_of, tenor) = realized_vol(trailing window) * VRP_factor,
floored. The VRP_factor (and window) are CALIBRATED by
scripts/options_parity_check.py against the ~7 weeks of real chain IV in
options_eod.parquet — do not treat the defaults here as authoritative until
that script has run and recorded a value.

Index options (^-prefixed underlyings) MAY be anchored to a vol index via the
optional vix_series hook; per-name options use the realized-vol proxy.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

IV_FLOOR = 0.05
DEFAULT_WINDOW = 21          # trading days
DEFAULT_VRP_FACTOR = 1.15    # placeholder; calibrated by options_parity_check.py
TRADING_DAYS = 252


def realized_vol(prices: pd.Series, window: int = DEFAULT_WINDOW) -> float:
    """Annualized close-to-close realized vol over the trailing `window` days."""
    s = prices.dropna()
    if len(s) < 3:
        return IV_FLOOR
    rets = s.pct_change().dropna().iloc[-window:]
    if len(rets) < 2:
        return IV_FLOOR
    return float(rets.std(ddof=1) * np.sqrt(TRADING_DAYS))


def synthetic_iv(prices: pd.Series, vrp_factor: float = DEFAULT_VRP_FACTOR,
                 window: int = DEFAULT_WINDOW) -> float:
    """Modeled IV for an underlying as of the last bar in `prices`."""
    rv = realized_vol(prices, window=window)
    return max(IV_FLOOR, float(rv) * float(vrp_factor))
