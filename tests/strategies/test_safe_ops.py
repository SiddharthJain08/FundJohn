"""safe_ops: single epsilon policy (EPS=1e-9), safe_div / safe_log / clip_pos."""
import warnings

import numpy as np
import pandas as pd

from strategies.safe_ops import EPS, clip_pos, safe_div, safe_log


def test_safe_div_fills_tiny_and_zero_denominators_both_signs():
    a = pd.Series([1., 1., 1., 1.])
    b = pd.Series([0., -1e-12, 1e-12, 2.])
    pd.testing.assert_series_equal(safe_div(a, b), pd.Series([0., 0., 0., 0.5]))
    pd.testing.assert_series_equal(
        safe_div(a, b, fill=np.nan), pd.Series([np.nan, np.nan, np.nan, 0.5]))


def test_safe_div_nan_denominator_stays_nan():
    out = safe_div(pd.Series([1., 1.]), pd.Series([np.nan, 2.]))
    assert np.isnan(out.iloc[0]) and out.iloc[1] == 0.5
    assert np.isnan(safe_div(1.0, float('nan')))
    assert np.isnan(safe_div(np.array([1.0]), np.array([np.nan]))[0])


def test_safe_div_dataframe_keeps_labels():
    idx = pd.to_datetime(['2026-01-02', '2026-01-05'])
    a = pd.DataFrame({'ZZTEST1': [1., 2.], 'ZZTEST2': [3., 4.]}, index=idx)
    b = pd.DataFrame({'ZZTEST1': [0., 2.], 'ZZTEST2': [1., 0.]}, index=idx)
    out = safe_div(a, b)
    assert isinstance(out, pd.DataFrame)
    assert out.index.equals(idx) and list(out.columns) == ['ZZTEST1', 'ZZTEST2']
    assert out.loc[idx[0], 'ZZTEST1'] == 0.0 and out.loc[idx[1], 'ZZTEST1'] == 1.0
    assert out.loc[idx[0], 'ZZTEST2'] == 3.0 and out.loc[idx[1], 'ZZTEST2'] == 0.0


def test_safe_div_scalar():
    assert safe_div(1.0, 0.0) == 0.0
    assert safe_div(1.0, 4.0) == 0.25
    assert isinstance(safe_div(1.0, 0.0), float)


def test_safe_log():
    out = safe_log(pd.Series([np.e, 0., -1., 1e-10]))
    pd.testing.assert_series_equal(out, pd.Series([1., np.nan, np.nan, np.nan]))
    assert np.isnan(safe_log(0.0)) and safe_log(np.e) == 1.0


def test_clip_pos():
    out = clip_pos(pd.Series([-1., 0., 2., np.nan]))
    pd.testing.assert_series_equal(out, pd.Series([1e-9, 1e-9, 2., np.nan]))
    assert EPS == 1e-9


def test_no_runtime_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        z = pd.Series([0., -1., 0.])
        safe_div(z, z)
        safe_div(np.zeros(3), np.zeros(3))
        safe_log(z)
        safe_log(np.array([0., -1.]))
        clip_pos(z)
        safe_div(0.0, 0.0)
        safe_log(-1.0)
        clip_pos(-1.0)
        safe_div(pd.DataFrame({'a': z}), pd.DataFrame({'a': z}))
