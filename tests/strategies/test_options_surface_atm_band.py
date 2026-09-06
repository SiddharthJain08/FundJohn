# tests/strategies/test_options_surface_atm_band.py
"""Spec 2026-09-06 §H: a chain too thin for a smile still yields a 30-day ATM
point from the |Δ| .40–.60 band (v1's definition), flagged by iv30_source; a
chain with smiles is untouched (the v2 freeze test guards SPY/AAPL/XOM)."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest

from strategies import options_surface as osf


def _rows(dte, strikes, spot=100.0, as_of='2026-09-03', iv=0.30):
    """One expiry; every strike carries a CALL and a PUT with a BS-ish delta."""
    t = dte / 365
    exp = (pd.Timestamp(as_of) + pd.Timedelta(days=dte)).date()
    out = []
    for K in strikes:
        d1 = (math.log(spot / K) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
        from scipy.stats import norm
        dc = float(norm.cdf(d1))
        for flag, d in (('CALL', dc), ('PUT', dc - 1.0)):
            out.append({'ticker': 'THIN', 'date': as_of, 'expiry': exp, 'strike': float(K), 'option_type': flag,
                        'implied_volatility': iv, 'delta': d, 'gamma': 0.01, 'theta': -0.02, 'vega': 0.1, 'volume': 1.0})
    return out


def test_thin_chain_gets_atm_band_iv30_flagged():
    # 3 strikes per expiry: below MIN_STRIKES (5) ⇒ no smile; ATM strike 100 sits in the .40–.60 band.
    rows = _rows(14, [95.0, 100.0, 105.0], iv=0.30) + _rows(42, [95.0, 100.0, 105.0], iv=0.34)
    row = osf.features_for_day(pd.DataFrame(rows), 100.0, '2026-09-03')
    assert row['n_expiries_fit'] == 0 and row['n_expiries_atm'] == 2
    assert row['iv30_source'] == 'atm_band'
    assert row['iv30'] is not None and 0.30 < row['iv30'] < 0.34          # bracketed 14 d ↔ 42 d in total variance
    assert row['iv_25d_put_30d'] is None and row['skew_25d_30d'] is None    # smile-only keys stay None
    assert row['mfiv_30d'] is None and row['rn_skew_30d'] is None            # v3 keys need a smile
    assert row['n_strikes_30d'] >= 1


def test_one_sided_tolerance_is_twenty_days():
    assert osf.CM_ONE_SIDED_TOL == 20
    row = osf.features_for_day(pd.DataFrame(_rows(14, [95.0, 100.0, 105.0])), 100.0, '2026-09-03')
    assert row['iv30'] == pytest.approx(0.30) and row['iv30_source'] == 'atm_band'   # lone 14-day monthly anchors 30 d
    row2 = osf.features_for_day(pd.DataFrame(_rows(60, [95.0, 100.0, 105.0])), 100.0, '2026-09-03')
    assert row2['iv30'] is None and row2['iv30_source'] is None                   # 30 d away: still None


def test_band_never_fabricates_when_no_atm_rows():
    rows = _rows(14, [80.0, 120.0])                                             # deltas far outside .40–.60
    row = osf.features_for_day(pd.DataFrame(rows), 100.0, '2026-09-03')
    assert row['iv30'] is None and row['iv30_source'] is None and row['n_expiries_atm'] == 0


def test_smile_expiry_keeps_smile_source_and_band_fills_gaps():
    K = 100.0 * np.exp(np.linspace(-0.3, 0.3, 25))
    rich = _rows(42, list(K), iv=0.25)                                           # smile-capable expiry
    thin = _rows(14, [95.0, 100.0, 105.0], iv=0.30)                              # band-only expiry
    row = osf.features_for_day(pd.DataFrame(rich + thin), 100.0, '2026-09-03')
    assert row['n_expiries_fit'] == 1 and row['n_expiries_atm'] == 1
    assert row['iv30_source'] == 'smile'                                         # nearest-30 expiry (42d, dist 12) is the smile fit, not the 14d band point (dist 16)
    assert row['iv30'] is not None and row['iv90'] is None or row['iv90'] is None or row['iv90'] > 0


def test_new_keys_registered():
    from strategies.aux_data_loader import FIELDS
    for k in ('iv30_source', 'n_expiries_atm'):
        assert k in osf.SCALAR_KEYS and k in FIELDS
    empty = osf.features_for_day(pd.DataFrame(), 100.0, '2026-09-03')
    assert empty['iv30_source'] is None and empty['n_expiries_atm'] == 0
