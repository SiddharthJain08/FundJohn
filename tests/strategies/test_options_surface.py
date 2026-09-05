# tests/strategies/test_options_surface.py
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from strategies import options_surface as osf


def _svi_iv(k, a=0.002, b=0.03, rho=-0.4, m=0.0, sig=0.1, t=30 / 365):
    w = a + b * (rho * (k - m) + math.sqrt((k - m) ** 2 + sig ** 2))
    return math.sqrt(w / t)


def _bs_delta(flag, S, K, t, iv):
    d1 = (math.log(S / K) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    return norm.cdf(d1) if flag == 'CALL' else norm.cdf(d1) - 1.0


def _chain(spot=100.0, as_of='2026-09-03', dtes=(10, 30, 60, 90), strikes=None, iv_fn=_svi_iv):
    strikes = strikes if strikes is not None else np.arange(70, 131, 2.5)
    rows = []
    for dte in dtes:
        t = max(dte, 1) / 365
        exp = (pd.Timestamp(as_of) + pd.Timedelta(days=dte)).date()
        for K in strikes:
            k = math.log(K / spot)
            iv = iv_fn(k, t=t)
            for flag in ('CALL', 'PUT'):
                d = _bs_delta(flag, spot, K, t, iv)
                rows.append({'ticker': 'ZZZT', 'date': as_of, 'expiry': exp, 'strike': float(K),
                             'option_type': flag, 'implied_volatility': iv, 'delta': d,
                             'gamma': 0.01, 'theta': -0.02, 'vega': 0.1, 'volume': 10.0, 'close': 1.0,
                             'open_interest': None})
    return pd.DataFrame(rows)


def test_prepare_chain_bands_dte_and_drops_zero_greeks():
    df = _chain(dtes=(0, 5, 30, 150))
    zero = df.iloc[[0]].copy(); zero[['delta', 'gamma', 'theta', 'vega']] = 0.0
    df = pd.concat([df, zero], ignore_index=True)
    out = osf.prepare_chain(df, '2026-09-03')
    assert sorted(out['dte'].unique()) == [5, 30]
    assert not ((out[['delta', 'gamma', 'theta', 'vega']].fillna(0) == 0).all(axis=1)).any()
    assert set(out['option_type'].unique()) <= {'CALL', 'PUT'}


def test_fit_smile_recovers_atm_and_25d_points():
    spot, dte = 100.0, 30
    strikes = np.arange(70, 131, 2.5)
    ivs = np.array([_svi_iv(math.log(K / spot)) for K in strikes])
    fit = osf.fit_smile(strikes, ivs, spot, dte)
    assert fit is not None and fit.n_strikes == len(strikes)
    assert fit.atm_iv == pytest.approx(_svi_iv(0.0), abs=1e-3)
    t = dte / 365
    # 25Δ put: find the strike whose BS put delta is -0.25 on the true smile, compare IVs
    ks = np.linspace(-0.3, 0.3, 6001)
    put_deltas = np.array([_bs_delta('PUT', spot, spot * math.exp(k), t, _svi_iv(k)) for k in ks])
    k_put = ks[np.argmin(np.abs(put_deltas + 0.25))]
    call_deltas = np.array([_bs_delta('CALL', spot, spot * math.exp(k), t, _svi_iv(k)) for k in ks])
    k_call = ks[np.argmin(np.abs(call_deltas - 0.25))]
    assert fit.iv_25d_put == pytest.approx(_svi_iv(k_put), abs=2e-3)
    assert fit.iv_25d_call == pytest.approx(_svi_iv(k_call), abs=2e-3)
    assert fit.iv_25d_put > fit.atm_iv > fit.iv_25d_call     # negative skew


def test_fit_smile_rejects_thin_or_one_sided_grids():
    assert osf.fit_smile(np.array([90, 95, 100, 105.0]), np.array([0.3, 0.28, 0.27, 0.26]), 100.0, 30) is None
    assert osf.fit_smile(np.array([101, 103, 105, 107, 109.0]), np.array([0.26] * 5), 100.0, 30) is None


def test_constant_maturity_interpolates_total_variance_and_one_sided_rule():
    f = lambda dte, iv: osf.SmileFit(dte=dte, t=dte / 365, atm_iv=iv, iv_25d_put=iv + 0.02, iv_25d_call=iv - 0.01, n_strikes=9, k_min=-0.3, k_max=0.3)
    fits = {20: f(20, 0.20), 40: f(40, 0.30)}
    v20, v40 = 0.20 ** 2 * 20 / 365, 0.30 ** 2 * 40 / 365
    vt = v20 + (v40 - v20) * (30 - 20) / (40 - 20)
    assert osf.constant_maturity(fits, 30, 'atm_iv') == pytest.approx(math.sqrt(vt / (30 / 365)))
    assert osf.constant_maturity({30: f(30, 0.25)}, 30, 'atm_iv') == pytest.approx(0.25)
    assert osf.constant_maturity({38: f(38, 0.25)}, 30, 'atm_iv') == pytest.approx(0.25)     # one-sided within 10 d
    assert osf.constant_maturity({45: f(45, 0.25)}, 30, 'atm_iv') is None                     # too far
    assert osf.constant_maturity({}, 30, 'atm_iv') is None


def test_features_for_day_keys_and_values():
    chain = _chain()
    row = osf.features_for_day(chain, 100.0, '2026-09-03')
    for k in ['iv30', 'iv90', 'near_iv', 'far_iv', 'ts_ratio', 'iv_25d_put_30d', 'iv_25d_call_30d',
              'skew_25d_30d', 'rr_25d_30d', 'skew_20d', 'iv_spread', 'term_slope', 'gamma_atm',
              'theta_atm', 'call_volume', 'put_volume', 'volume', 'pc_ratio', 'spot', 'last_price',
              'expiry_date', 'n_expiries_fit', 'n_strikes_30d', 'options_features_version']:
        assert k in row, k
    assert row['options_features_version'] == 2
    assert row['iv30'] == pytest.approx(_svi_iv(0.0), abs=2e-3)
    assert row['near_iv'] == row['iv30'] and row['far_iv'] == row['iv90']
    assert row['ts_ratio'] == pytest.approx(row['iv30'] / row['iv90'])
    assert row['skew_20d'] == row['skew_25d_30d'] == pytest.approx(row['iv_25d_put_30d'] - row['iv30'])
    assert row['pc_ratio'] == pytest.approx(1.0) and row['volume'] == pytest.approx(chain['volume'].sum())
    assert row['expiry_date'] == '2026-09-13'        # front usable expiry (10 d)
    assert row['n_expiries_fit'] == 4 and row['spot'] == 100.0 == row['last_price']
    assert row['iv_spread'] == pytest.approx(0.0, abs=1e-9)   # symmetric synthetic chain


def test_features_for_day_without_spot_or_empty_chain():
    empty = osf.features_for_day(_chain().iloc[0:0], 100.0, '2026-09-03')
    assert empty['iv30'] is None and empty['n_expiries_fit'] == 0
    nospot = osf.features_for_day(_chain(), None, '2026-09-03')
    assert nospot['iv30'] is None and nospot['pc_ratio'] == pytest.approx(1.0)
