"""Spec 2026-09-06 B.3: surface → vix_term → realized, with dte-aware points."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest


def _surface(tmp_path, monkeypatch, rows):
    p = tmp_path / 'options_surface.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_PATH', str(p))
    from backtest import synthetic_iv as si
    si.clear_cache()
    return si


def _px(n=120, seed=0):
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)), index=pd.date_range('2026-04-01', periods=n, freq='B'))


def test_surface_tier_and_total_variance_interpolation(tmp_path, monkeypatch):
    si = _surface(tmp_path, monkeypatch, [{'ticker': 'ZZZT', 'date': pd.Timestamp('2026-08-03').date(), 'iv30': 0.20, 'iv90': 0.30}])
    iv30, src = si.synthetic_iv_detail(_px(), underlying='ZZZT', as_of='2026-08-04', dte=30)
    assert (iv30, src) == (pytest.approx(0.20), 'surface')
    iv60, _ = si.synthetic_iv_detail(_px(), underlying='ZZZT', as_of='2026-08-04', dte=60)
    w = 0.04 * (30 / 365) + (0.09 * (90 / 365) - 0.04 * (30 / 365)) * ((60 - 30) / 365) / ((90 - 30) / 365)
    assert iv60 == pytest.approx(math.sqrt(w / (60 / 365)))
    assert si.surface_iv('ZZZT', '2026-08-04', 200) == pytest.approx(0.30)       # flat beyond 90
    assert si.surface_iv('ZZZT', '2026-08-04', 10) == pytest.approx(0.20)        # flat below 30


def test_surface_tier_respects_asof_tolerance_and_missing_iv90(tmp_path, monkeypatch):
    si = _surface(tmp_path, monkeypatch, [{'ticker': 'ZZZT', 'date': pd.Timestamp('2026-08-03').date(), 'iv30': 0.20, 'iv90': None}])
    assert si.surface_iv('ZZZT', '2026-08-10', 60) == pytest.approx(0.20)        # 7 days: still inside, iv90 None ⇒ flat iv30
    assert si.surface_iv('ZZZT', '2026-08-11', 60) is None                       # 8 days: stale
    assert si.surface_iv('ZZZT', '2026-08-01', 30) is None                       # before the first row
    assert si.surface_iv('NOPE', '2026-08-04', 30) is None


def test_realized_tier_when_nothing_else_applies(tmp_path, monkeypatch):
    si = _surface(tmp_path, monkeypatch, [{'ticker': 'ZZZT', 'date': pd.Timestamp('2026-08-03').date(), 'iv30': 0.20, 'iv90': 0.30}])
    px = _px()
    iv, src = si.synthetic_iv_detail(px, vrp_factor=1.2, underlying='NOPE', as_of=px.index[-1])
    from backtest.synthetic_iv import realized_vol
    assert src == 'realized' and iv == pytest.approx(max(0.05, realized_vol(px) * 1.2))
    assert si.synthetic_iv(px, vrp_factor=1.2, underlying='NOPE', as_of=px.index[-1]) == iv
    assert si.synthetic_iv_detail(px)[1] == 'realized'                            # no underlying/as_of


def test_vix_term_point_interpolates_9d_and_30d(tmp_path, monkeypatch):
    from backtest import vol_index as vi
    p = tmp_path / 'vol_indices.parquet'
    pd.DataFrame([{'date': pd.Timestamp('2026-08-03').date(), 'vix_close': 20.0, 'vvix_close': 90.0, 'vix9d_close': 16.0}]).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_VOL_INDICES_PARQUET', str(p))
    vi._vix9d_series.cache_clear()
    monkeypatch.setattr(vi, '_vix_series', lambda: pd.Series([0.20], index=pd.DatetimeIndex([pd.Timestamp('2026-08-03')])))
    try:
        assert vi.vix_term_point('2026-08-03', 30) == pytest.approx(0.20)
        assert vi.vix_term_point('2026-08-03', 45) == pytest.approx(0.20)         # flat above 30
        assert vi.vix_term_point('2026-08-03', 9) == pytest.approx(0.16)
        assert vi.vix_term_point('2026-08-03', 5) == pytest.approx(0.16)          # flat below 9
        mid = vi.vix_term_point('2026-08-03', 20)
        assert 0.16 < mid < 0.20
        assert mid == pytest.approx(vi.interp_total_variance(9, 0.16, 30, 0.20, 20))
        assert vi.vix_anchored_iv('SPY', '2026-08-03', 20) == pytest.approx(vi.OPTION_UNDERLYING_BETA['SPY'] * mid)
        assert vi.vix_anchored_iv('SPY', '2026-08-03') == pytest.approx(vi.OPTION_UNDERLYING_BETA['SPY'] * 0.20)
        assert vi.vix_term_point('2026-07-01', 30) is None                        # before any VIX
    finally:
        vi._vix9d_series.cache_clear()


def test_vix_term_tier_is_used_for_supported_names_without_surface(tmp_path, monkeypatch):
    si = _surface(tmp_path, monkeypatch, [{'ticker': 'ZZZT', 'date': pd.Timestamp('2026-08-03').date(), 'iv30': 0.20, 'iv90': 0.30}])
    from backtest import vol_index as vi
    monkeypatch.setattr(vi, 'vix_term_point', lambda as_of, dte=30: 0.25)
    iv, src = si.synthetic_iv_detail(_px(), underlying='SPY', as_of='2026-08-04', dte=30)
    assert src == 'vix_term' and iv == pytest.approx(vi.OPTION_UNDERLYING_BETA['SPY'] * 0.25)


def test_interp_total_variance_endpoints_and_monotone():
    from backtest.vol_index import interp_total_variance
    assert interp_total_variance(30, 0.2, 90, 0.3, 30) == 0.2
    assert interp_total_variance(30, 0.2, 90, 0.3, 90) == 0.3
    assert interp_total_variance(30, 0.2, 90, 0.3, 1) == 0.2 and interp_total_variance(30, 0.2, 90, 0.3, 400) == 0.3
    a, b = interp_total_variance(30, 0.2, 90, 0.3, 45), interp_total_variance(30, 0.2, 90, 0.3, 75)
    assert 0.2 < a < b < 0.3
