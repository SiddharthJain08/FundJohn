"""SP-3.1 Phase B: crypto regime core — pure functions (no I/O).

Features are price-derived realized-vol measures (no crypto IV index exists on
our providers). Column 0 (`btc_rv_24h`) is the primary vol axis so HMM state
ordering by means_[:,0] maps ascending vol -> LOW_VOL..CRISIS (mirrors the
equity HMM ordering by VIX mean)."""
from __future__ import annotations
import pickle
import numpy as np
import pandas as pd
from hmmlearn import hmm

CRYPTO_FEATURE_COLS = [
    'btc_rv_24h', 'btc_rv_168h', 'btc_vol_term_slope', 'btc_ret_24h', 'eth_btc_dispersion',
]
STATE_NAMES_ORDERED = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
_ANN = np.sqrt(24 * 365)  # hourly -> annualized vol factor


def _rv(close: pd.Series, window: int) -> pd.Series:
    """Annualized rolling realized vol of hourly log returns."""
    logret = np.log(close).diff()
    return logret.rolling(window).std() * _ANN * 100.0


def compute_features(bars: pd.DataFrame) -> pd.DataFrame:
    """bars: long-format rows with columns ticker, ts_utc, close (>= BTC-USD;
    ETH-USD optional). Returns a per-timestamp feature frame indexed by ts_utc,
    columns == CRYPTO_FEATURE_COLS, NaN rows dropped."""
    wide = bars.pivot_table(index='ts_utc', columns='ticker', values='close', aggfunc='last')
    wide = wide.sort_index()
    btc = wide['BTC-USD']
    eth = wide['ETH-USD'] if 'ETH-USD' in wide.columns else btc  # degrade: dispersion -> 0
    feats = pd.DataFrame(index=wide.index)
    feats['btc_rv_24h'] = _rv(btc, 24)
    feats['btc_rv_168h'] = _rv(btc, 168)
    feats['btc_vol_term_slope'] = feats['btc_rv_24h'] / feats['btc_rv_168h'].replace(0, np.nan)
    feats['btc_ret_24h'] = btc.pct_change(24) * 100.0
    feats['eth_btc_dispersion'] = _rv(eth, 24) - feats['btc_rv_24h']
    feats = feats[CRYPTO_FEATURE_COLS].replace([np.inf, -np.inf], np.nan).dropna()
    return feats


def fit_hmm(feats: pd.DataFrame, *, n_iter: int = 200, seed: int = 42):
    """Fit a 4-state full-covariance Gaussian HMM on the feature frame.
    Mirrors scripts/run_market_state.py:144-146."""
    model = hmm.GaussianHMM(n_components=4, covariance_type='full',
                            n_iter=n_iter, random_state=seed, tol=1e-4)
    model.fit(feats[CRYPTO_FEATURE_COLS].values)
    return model


def state_names(model) -> dict[int, str]:
    """Map raw HMM state index -> regime name by ascending feature-0 (rv_24h)
    mean. Mirrors run_market_state.py:163-170 (which orders by VIX mean)."""
    order = np.argsort(model.means_[:, 0])
    return {int(order[i]): STATE_NAMES_ORDERED[i] for i in range(4)}


def score_latest(model, feats: pd.DataFrame) -> tuple[str, float, dict]:
    """Return (label, confidence, {name: prob}) for the most-recent row."""
    names = state_names(model)
    seq = model.predict(feats[CRYPTO_FEATURE_COLS].values)
    probs = model.predict_proba(feats[CRYPTO_FEATURE_COLS].values)[-1]
    raw = int(seq[-1])
    label = names[raw]
    named = {names[i]: round(float(probs[i]), 4) for i in range(4)}
    return label, named[label], named


def save_model(model, path) -> None:
    with open(path, 'wb') as f:
        pickle.dump(model, f)


def load_model(path):
    with open(path, 'rb') as f:
        return pickle.load(f)
