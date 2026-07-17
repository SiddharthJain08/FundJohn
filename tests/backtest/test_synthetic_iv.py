import sys
from pathlib import Path
import numpy as np, pandas as pd
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest.synthetic_iv import realized_vol, synthetic_iv


def _series(n=300, daily_sigma=0.01, seed=0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, daily_sigma, n)
    px = 100 * np.cumprod(1 + rets)
    return pd.Series(px, index=pd.date_range('2020-01-01', periods=n, freq='D'))


def test_realized_vol_recovers_input_sigma():
    s = _series(daily_sigma=0.01)
    rv = realized_vol(s, window=60)
    assert 0.10 < rv < 0.22


def test_synthetic_iv_applies_vrp_markup():
    s = _series(daily_sigma=0.01)
    rv = realized_vol(s, window=60)
    iv = synthetic_iv(s, vrp_factor=1.2, window=60)
    assert abs(iv - rv * 1.2) < 1e-9


def test_synthetic_iv_has_floor():
    s = pd.Series([100.0] * 100, index=pd.date_range('2020-01-01', periods=100))
    iv = synthetic_iv(s, vrp_factor=1.0, window=60)
    assert iv >= 0.05
