# tests/strategies/test_options_surface_mfiv.py
"""Spec 2026-09-06 §A.1–A.2 oracles: a flat smile is lognormal (MFIV = σ, RN
skew 0, RN kurtosis 3, tails = Black digitals); a left-skewed SVI smile prices
its wings above ATM and its down-tail above its up-tail."""
from __future__ import annotations
import math
import numpy as np
import pytest
from scipy.stats import norm

from strategies import options_surface as osf


def _flat_fit(sigma=0.25, dte=30, spot=100.0):
    K = spot * np.exp(np.linspace(-0.4, 0.4, 17))
    return osf.fit_smile(K, np.full(len(K), sigma), spot, dte)


def _svi_fit(dte=30, spot=100.0, a=0.002, b=0.03, rho=-0.6, m=0.0, s=0.1):
    t = dte / 365
    k = np.linspace(-0.3, 0.3, 25)
    w = a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + s * s))
    return osf.fit_smile(spot * np.exp(k), np.sqrt(w / t), spot, dte)


def _black_p_below(k, sigma, t):
    d1 = (-k + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))
    return norm.cdf(-(d1 - sigma * math.sqrt(t)))


def test_flat_smile_mfiv_equals_sigma():
    f = _flat_fit()
    assert f.mfiv == pytest.approx(0.25, rel=1e-4)


def test_flat_smile_rn_moments_are_lognormal():
    f = _flat_fit()
    # Tightened to the measured quadrature error (2.3e-5 / 2.99937) so a real
    # regression in the strip cannot hide inside a loose band (final review M1).
    assert abs(f.rn_skew) < 1e-3
    assert f.rn_kurt == pytest.approx(3.0, abs=1e-2)


def test_flat_smile_tails_equal_black_digitals():
    f = _flat_fit()
    t = 30 / 365
    assert f.rn_p_dn10 == pytest.approx(_black_p_below(math.log(0.9), 0.25, t), abs=1e-9)
    assert f.rn_p_up10 == pytest.approx(1.0 - _black_p_below(math.log(1.1), 0.25, t), abs=1e-9)
    assert 0.0 < f.rn_p_dn10 < 0.5 and 0.0 < f.rn_p_up10 < 0.5


def test_left_skewed_smile_prices_wings_and_down_tail():
    f = _svi_fit()
    assert f.mfiv > f.atm_iv
    assert f.rn_skew < 0.0
    assert f.rn_kurt > 3.0
    assert 0.0 <= f.rn_p_up10 < f.rn_p_dn10 <= 1.0


def test_wings_are_flat_beyond_observed_strikes():
    f = _svi_fit()
    K = 100.0 * np.exp(np.linspace(-0.3, 0.3, 25)); t = 30 / 365
    w = 0.002 + 0.03 * (-0.6 * np.log(K / 100.0) + np.sqrt(np.log(K / 100.0) ** 2 + 0.01))
    from scipy.interpolate import PchipInterpolator
    smile = PchipInterpolator(np.log(K / 100.0), np.sqrt(w / t), extrapolate=False)
    far = osf._sigma_on(smile, np.array([-0.9, 0.9]), f.k_min, f.k_max, f.atm_iv)
    assert far[0] == pytest.approx(float(smile(f.k_min)))
    assert far[1] == pytest.approx(float(smile(f.k_max)))


def test_strip_features_never_raises():
    def boom(k):
        raise RuntimeError('degenerate smile')
    out = osf.strip_features(boom, -0.1, 0.1, 0.2, 30 / 365)
    assert out == {'mfiv': None, 'rn_skew': None, 'rn_kurt': None, 'rn_p_dn10': None, 'rn_p_up10': None}
    assert osf.strip_features(lambda k: np.asarray(k) * 0 + 0.2, -0.1, 0.1, 0.0, 30 / 365)['mfiv'] is None


def test_fit_smile_none_paths_unchanged():
    assert osf.fit_smile([100.0, 101.0], [0.2, 0.2], 100.0, 30) is None          # < MIN_STRIKES
    assert osf.fit_smile(np.arange(101, 110), np.full(9, 0.2), 100.0, 30) is None  # no strike below spot


V3_KEYS = ['mfiv_30d', 'mfiv_90d', 'mf_tail_premium_30d',
           'rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d']


def _fixture_row(ticker='SPY'):
    import json
    from pathlib import Path
    import pandas as pd
    fix = Path(__file__).resolve().parents[1] / 'fixtures'
    chain = pd.read_parquet(fix / 'options_chain_2026-09-03.parquet')
    meta = json.load(open(fix / 'options_chain_2026-09-03_spots.json'))
    ch = chain[chain['ticker'] == ticker]
    ch = ch.assign(date=pd.to_datetime(ch['date']))
    return osf.features_for_day(ch, meta['spots'][ticker], pd.Timestamp('2026-09-03'))


def test_v3_keys_are_scalar_keys_and_aux_fields():
    from strategies.aux_data_loader import FIELDS
    for k in V3_KEYS:
        assert k in osf.SCALAR_KEYS and k in FIELDS, k
    assert osf.OPTIONS_FEATURES_VERSION == 3


def test_spy_fixture_carries_v3_values():
    row = _fixture_row('SPY')
    assert row['options_features_version'] == 3
    assert row['mfiv_30d'] is not None and row['iv30'] is not None
    assert 0.0 <= row['mf_tail_premium_30d'] <= 0.05          # index smile: wings a few vol points rich
    assert row['mfiv_30d'] == pytest.approx(row['iv30'] + row['mf_tail_premium_30d'])
    assert row['rn_skew_30d'] < 0.0                             # left-skewed index smile
    assert row['rn_kurt_30d'] > 3.0
    assert 0.0 < row['rn_p_dn10_30d'] < 0.2 and 0.0 <= row['rn_p_up10_30d'] < 0.2
    assert row['mfiv_90d'] is None or row['mfiv_90d'] > 0.0


def test_v3_keys_none_without_a_30d_expiry():
    import pandas as pd
    K = 100.0 * np.exp(np.linspace(-0.3, 0.3, 25)); t = 60 / 365
    rows = [{'ticker': 'ZZZT', 'date': '2026-09-03', 'expiry': (pd.Timestamp('2026-09-03') + pd.Timedelta(days=60)).date(),
             'strike': float(k), 'option_type': f, 'implied_volatility': 0.25, 'delta': 0.5 if f == 'CALL' else -0.5,
             'gamma': 0.01, 'theta': -0.02, 'vega': 0.1, 'volume': 1.0}
            for k in K for f in ('CALL', 'PUT')]
    row = osf.features_for_day(pd.DataFrame(rows), 100.0, '2026-09-03')
    assert row['n_expiries_fit'] == 1
    for k in ('rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d', 'mfiv_30d', 'mf_tail_premium_30d'):
        assert row[k] is None, k                                # |60 − 30| > 15 and > CM_ONE_SIDED_TOL


def test_empty_chain_row_has_v3_keys_as_none():
    import pandas as pd
    row = osf.features_for_day(pd.DataFrame(), 100.0, '2026-09-03')
    assert all(row[k] is None for k in V3_KEYS) and row['options_features_version'] == 3
