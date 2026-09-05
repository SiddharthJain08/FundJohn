# tests/strategies/test_options_surface_series.py
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest

from strategies import options_surface as osf


def test_rv_series_is_log_return_std_annualised():
    idx = pd.bdate_range('2026-01-02', periods=40)
    closes = pd.Series(100 * np.exp(np.cumsum(np.full(40, 0.01))), index=idx)
    rv = osf.rv_series_from_closes(closes)
    assert rv.index.equals(idx)
    assert math.isnan(rv.iloc[19])                  # needs 20 returns → first value at position 20
    assert rv.iloc[-1] == pytest.approx(0.0, abs=1e-12)   # constant log return → zero vol


def _frame(n, iv):
    idx = pd.bdate_range('2025-06-02', periods=n)
    return pd.DataFrame({'date': idx, 'iv30': iv, 'pc_ratio': np.linspace(0.8, 1.2, n), 'rv_20': np.full(n, 0.15)})


def test_series_frame_iv_rank_none_below_min_obs_then_percentile():
    df = _frame(25, np.linspace(0.10, 0.34, 25))
    out = osf.series_frame(df)
    assert out['iv_rank'].iloc[18] is None or pd.isna(out['iv_rank'].iloc[18])
    assert out['iv_rank'].iloc[19] == pytest.approx(100.0)          # 20th obs is the max of its window
    assert out['iv_rank'].iloc[-1] == pytest.approx(100.0)
    df2 = _frame(30, np.r_[np.linspace(0.30, 0.10, 29), 0.20])
    assert osf.series_frame(df2)['iv_rank'].iloc[-1] == pytest.approx(pd.Series(df2['iv30']).rank(pct=True).iloc[-1] * 100)


def test_series_frame_histories_and_zscore():
    df = _frame(80, 0.2 + 0.05 * np.sin(np.arange(80) / 5))
    out = osf.series_frame(df)
    last = out.iloc[-1]
    assert len(last['iv_rank_history']) == osf.HIST_LEN and len(last['vrp_history']) == osf.HIST_LEN
    assert len(last['hv20_history']) == osf.HIST_LEN and len(last['pc_ratio_history']) == osf.HIST_LEN
    assert last['vrp'] == pytest.approx(df['iv30'].iloc[-1] - 0.15)
    assert last['vrp_zscore'] is not None and math.isfinite(last['vrp_zscore'])
    assert out['vrp_zscore'].iloc[5] is None or pd.isna(out['vrp_zscore'].iloc[5])


def test_series_features_matches_last_row_of_series_frame():
    idx = pd.bdate_range('2025-06-02', periods=60)
    hist = pd.DataFrame({'date': idx[:-1], 'iv30': np.linspace(0.2, 0.3, 59), 'pc_ratio': 1.0})
    rv = pd.Series(np.full(60, 0.18), index=idx)
    today = {'date': idx[-1], 'iv30': 0.25, 'pc_ratio': 1.1}
    feat = osf.series_features(today, hist, rv)
    full = osf.series_frame(pd.concat([hist.assign(rv_20=0.18), pd.DataFrame([{**today, 'rv_20': 0.18}])], ignore_index=True)).iloc[-1]
    assert feat['iv_rank'] == pytest.approx(full['iv_rank'])
    assert feat['rv_20'] == pytest.approx(0.18) and feat['vrp'] == pytest.approx(0.07)
    assert feat['iv_rank_history'] == list(full['iv_rank_history'])


# ── Final fix wave 2026-09-05, F1: rv_20 is mapped AS-OF, not by exact date ──
def test_series_features_rv_asof_when_closes_lag_the_chain_date():
    """Production intraday overlay: the chain row is dated today, prices.parquet
    still ends at T-1. The exact-date map returned NaN and silently dropped
    rv_20/vrp/vrp_zscore from the live dict."""
    idx = pd.bdate_range('2025-06-02', periods=60)
    today = idx[-1]                                    # the chain/surface date
    hist = pd.DataFrame({'date': idx[:-1], 'iv30': np.linspace(0.2, 0.3, 59), 'pc_ratio': 1.0})
    rv = pd.Series(np.full(59, 0.18), index=idx[:-1])  # closes END ONE SESSION EARLY
    feat = osf.series_features({'date': today, 'iv30': 0.25, 'pc_ratio': 1.1}, hist, rv)
    assert feat['rv_20'] == pytest.approx(0.18)        # T-1's realized vol carried forward
    assert feat['vrp'] == pytest.approx(0.07)
    assert feat['vrp_zscore'] is not None
    assert len(feat['hv20_history']) == osf.HIST_LEN


def test_series_features_asof_equals_exact_when_close_is_same_day():
    """With a same-day close the as-of map must reproduce the exact-date value
    bit-for-bit - that equality is what keeps the backtest panel unmoved."""
    idx = pd.bdate_range('2025-06-02', periods=60)
    hist = pd.DataFrame({'date': idx[:-1], 'iv30': np.linspace(0.2, 0.3, 59), 'pc_ratio': 1.0})
    rv = pd.Series(np.linspace(0.10, 0.25, 60), index=idx)     # every date present
    today = {'date': idx[-1], 'iv30': 0.25, 'pc_ratio': 1.1}
    feat = osf.series_features(today, hist, rv)
    exact = osf.series_frame(pd.concat(
        [hist.assign(rv_20=rv.to_numpy()[:-1]),
         pd.DataFrame([{**today, 'rv_20': rv.iloc[-1]}])], ignore_index=True)).iloc[-1]
    assert feat['rv_20'] == pytest.approx(float(exact['rv_20']))
    assert feat['vrp'] == pytest.approx(float(exact['vrp']))
    assert feat['hv20_history'] == list(exact['hv20_history'])


def test_series_features_rv_beyond_tolerance_is_none():
    """A stale close series (older than RV_ASOF_TOLERANCE) must NOT be carried
    forward - rv_20 stays None rather than quietly pricing off a fortnight ago."""
    idx = pd.bdate_range('2025-06-02', periods=60)
    hist = pd.DataFrame({'date': idx[:-1], 'iv30': np.linspace(0.2, 0.3, 59), 'pc_ratio': 1.0})
    rv = pd.Series(np.full(50, 0.18), index=idx[:50])          # ends ~14 calendar days early
    assert (idx[-1] - idx[49]) > osf.RV_ASOF_TOLERANCE
    feat = osf.series_features({'date': idx[-1], 'iv30': 0.25, 'pc_ratio': 1.1}, hist, rv)
    assert feat['rv_20'] is None and feat['vrp'] is None


def test_series_features_survives_unsorted_duplicated_and_empty_rv():
    """reindex(method='ffill') needs a sorted, unique index - series_features
    normalises rather than raising, and an empty series yields None."""
    idx = pd.bdate_range('2025-06-02', periods=60)
    hist = pd.DataFrame({'date': idx[:-1], 'iv30': np.linspace(0.2, 0.3, 59), 'pc_ratio': 1.0})
    shuffled = pd.Series(np.full(60, 0.18), index=idx)[::-1]
    shuffled = pd.concat([shuffled, pd.Series([0.18], index=[idx[-1]])])    # duplicate label
    feat = osf.series_features({'date': idx[-1], 'iv30': 0.25, 'pc_ratio': 1.1}, hist, shuffled)
    assert feat['rv_20'] == pytest.approx(0.18)
    empty = osf.series_features({'date': idx[-1], 'iv30': 0.25, 'pc_ratio': 1.1}, hist, pd.Series(dtype=float))
    assert empty['rv_20'] is None and empty['vrp'] is None


# ── Final fix wave 2026-09-05, F4b: the sliding _history must be a no-op change ──
def _history_reference(s, window=20, min_len=5):
    """The O(n*window) loop _history replaced - the oracle, inlined."""
    vals = s.tolist()
    out = []
    for i in range(len(vals)):
        h = [float(v) for v in vals[max(0, i - window + 1):i + 1] if v is not None and not pd.isna(v)]
        out.append(h if len(h) >= min_len else None)
    return pd.Series(out, index=s.index, dtype=object)


@pytest.mark.parametrize('dtype', ['float', 'object'])
def test_history_matches_reference_loop(dtype):
    rng = np.random.default_rng(20260905)
    raw = rng.normal(size=200)
    holes = rng.random(200) < 0.25
    if dtype == 'float':
        s = pd.Series(np.where(holes, np.nan, raw))
    else:
        s = pd.Series([None if h else float(v) for h, v in zip(holes, raw)], dtype=object)
    got, want = osf._history(s), _history_reference(s)
    assert list(got.index) == list(want.index)
    for g, w in zip(got, want):
        assert (g is None) == (w is None)
        if g is not None:
            assert g == pytest.approx(w)
    for n in (0, 1, 4, 5, 21):
        empty = pd.Series(np.full(n, np.nan))
        assert list(osf._history(empty)) == list(_history_reference(empty))
