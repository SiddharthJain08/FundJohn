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


def test_compute_features_no_btc_returns_empty_frame():
    # Defensive guard: parquet without BTC-USD must not KeyError — returns an
    # empty, correctly-columned frame so the driver's len<200 guard catches it.
    eth_only = _synth_bars(ticker='ETH-USD', seed=9)
    feats = cr.compute_features(eth_only)
    assert list(feats.columns) == cr.CRYPTO_FEATURE_COLS
    assert len(feats) == 0


def test_fit_and_score_orders_states_by_vol_and_returns_label():
    # Two volatility regimes concatenated -> the high-vol tail must score
    # a more severe label than a low-vol prefix.
    low = _synth_bars(n=300, vol=0.002, seed=3)
    high = _synth_bars(n=300, vol=0.02, seed=4)
    high['ts_utc'] = pd.date_range('2026-03-01', periods=300, freq='h', tz='UTC') \
        .strftime('%Y-%m-%dT%H:%M:%SZ')
    bars = pd.concat([low, high], ignore_index=True)
    eth = bars.copy(); eth['ticker'] = 'ETH-USD'
    feats = cr.compute_features(pd.concat([bars, eth], ignore_index=True))
    model = cr.fit_hmm(feats)
    label, conf, probs = cr.score_latest(model, feats)
    assert label in cr.STATE_NAMES_ORDERED
    assert 0.0 <= conf <= 1.0
    assert set(probs.keys()) == set(cr.STATE_NAMES_ORDERED)
    names = cr.state_names(model)
    # Ordering is by ascending means_[:,0]; raw HMM indices are arbitrary, so
    # assert on the SORTED order: lowest-vol raw state -> LOW_VOL, highest -> CRISIS.
    order = np.argsort(model.means_[:, 0])
    assert names[int(order[0])] == 'LOW_VOL'
    assert names[int(order[-1])] == 'CRISIS'
    assert set(names.values()) == set(cr.STATE_NAMES_ORDERED)


def test_hysteresis_streak_and_confirmed_transition():
    # newest-first history, settled LOW_VOL (streak>=3), new CRISIS at conf 0.95
    hist = [
        {'state': 'CRISIS', 'fired_redeploy': False, 'hysteresis_streak': 1},
        {'state': 'LOW_VOL', 'fired_redeploy': False, 'hysteresis_streak': 5},
        {'state': 'LOW_VOL', 'fired_redeploy': False, 'hysteresis_streak': 4},
    ]
    streak = cr.hysteresis_streak(hist, 'CRISIS')
    fired, prior = cr.confirmed_transition(hist, 'CRISIS', streak, 0.95)
    assert fired is True and prior == 'LOW_VOL'
    fired2, _ = cr.confirmed_transition(hist, 'LOW_VOL', cr.hysteresis_streak(hist, 'LOW_VOL'), 0.99)
    assert fired2 is False
    fired3, _ = cr.confirmed_transition(hist, 'CRISIS', streak, 0.50)
    assert fired3 is False


def test_build_latest_json_shape():
    j = cr.build_latest_json(
        ts_utc='2026-05-26T07:00:00Z', state='HIGH_VOL', prior_state='LOW_VOL',
        confidence=0.83, streak=2,
        probs={'LOW_VOL': 0.05, 'TRANSITIONING': 0.10, 'HIGH_VOL': 0.83, 'CRISIS': 0.02},
        features={'btc_rv_24h': 80.0}, transition_tag='CRYPTO_HMM_LOW_VOL_HIGH_VOL',
        refit_performed=False)
    assert j['state'] == 'HIGH_VOL'
    assert j['confidence'] == 0.83
    assert j['prior_state'] == 'LOW_VOL'
    assert j['state_probabilities']['HIGH_VOL'] == 0.83
    assert j['transition_tag'] == 'CRYPTO_HMM_LOW_VOL_HIGH_VOL'
    assert j['fired_redeploy'] is False
