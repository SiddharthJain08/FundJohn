"""tests/test_crypto_regime.py — SP-3.1 Phase B crypto regime core (pure fns)."""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from regime import crypto_regime as cr  # noqa: E402


def _synth_bars(n=400, ticker='BTC-USD', base=50000.0, vol=0.005, seed=0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, vol, n)
    close = base * np.exp(np.cumsum(rets))
    ts = pd.date_range('2026-01-01', periods=n, freq='h', tz='UTC')
    return pd.DataFrame({'ticker': ticker, 'ts_utc': ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
                         'close': close})


def test_compute_features_columns_and_finiteness():
    btc = _synth_bars(ticker='BTC-USD', vol=0.004, seed=1)
    eth = _synth_bars(ticker='ETH-USD', vol=0.006, seed=2)
    feats = cr.compute_features(pd.concat([btc, eth], ignore_index=True))
    assert list(feats.columns) == cr.CRYPTO_FEATURE_COLS
    assert np.isfinite(feats.iloc[-1].values).all()
    assert feats.iloc[-1]['btc_vol_term_slope'] > 0


def test_first_feature_is_a_vol_so_state_ordering_works():
    assert cr.CRYPTO_FEATURE_COLS[0] == 'btc_rv_24h'
