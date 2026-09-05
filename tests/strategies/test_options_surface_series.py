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
