import sys
from pathlib import Path
from datetime import date
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pandas as pd
from backtest.vol_index import (vix_anchored_iv, is_supported_option_underlying,
                                 OPTION_UNDERLYING_BETA, _vix_series)
from backtest.synthetic_iv import synthetic_iv


def test_vix_series_loads_10y():
    s = _vix_series()
    assert len(s) > 2000  # ~10y of daily VIX
    assert 0.05 < s.median() < 0.40  # decimal vol, sane


def test_vix_anchored_iv_spy_matches_beta_times_vix():
    s = _vix_series()
    asof = s.index[-1]
    expected = OPTION_UNDERLYING_BETA['SPY'] * float(s.iloc[-1])
    assert abs(vix_anchored_iv('SPY', asof) - expected) < 1e-9


def test_unsupported_underlying_returns_none():
    assert vix_anchored_iv('AMD', pd.Timestamp('2024-01-02')) is None
    assert not is_supported_option_underlying('AMD')
    assert is_supported_option_underlying('SPY')


def test_synthetic_iv_uses_vix_when_supported():
    s = _vix_series(); asof = s.index[-1]
    px = pd.Series([400.0] * 30, index=pd.date_range('2020-01-01', periods=30))  # flat → low realized
    iv_anchored = synthetic_iv(px, underlying='SPY', as_of=asof)
    iv_realized = synthetic_iv(px)  # no underlying → realized path (floored ~0.05)
    assert abs(iv_anchored - OPTION_UNDERLYING_BETA['SPY'] * float(s.iloc[-1])) < 1e-9
    assert iv_anchored != iv_realized


def test_synthetic_iv_falls_back_for_unsupported():
    import numpy as np
    rng = np.random.default_rng(0)
    px = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 200)),
                   index=pd.date_range('2020-01-01', periods=200))
    iv = synthetic_iv(px, vrp_factor=1.2, underlying='AMD', as_of=px.index[-1])
    # AMD not supported → realized path
    from backtest.synthetic_iv import realized_vol
    assert abs(iv - max(0.05, realized_vol(px) * 1.2)) < 1e-9
