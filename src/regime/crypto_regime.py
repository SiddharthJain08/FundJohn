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


HYSTERESIS_TIERS = {
    'CRISIS': (1, 0.90), 'HIGH_VOL': (2, 0.80),
    'TRANSITIONING': (3, 0.70), 'LOW_VOL': (3, 0.70),
}
_DOWNWARD_TIER = (3, 0.70)
_STATE_RANK = {'LOW_VOL': 0, 'TRANSITIONING': 1, 'HIGH_VOL': 2, 'CRISIS': 3}


def hysteresis_streak(history: list[dict], current_state: str) -> int:
    """Consecutive most-recent rows matching current_state, +1 for this tick.
    history is newest-first. Mirrors run_intraday_market_state.py:365-374."""
    n = 0
    for row in history:
        if row.get('state') == current_state:
            n += 1
        else:
            break
    return n + 1


def _is_upward(prior_state, new_state) -> bool:
    if prior_state is None:
        return False
    return _STATE_RANK.get(new_state, -1) > _STATE_RANK.get(prior_state, -1)


def _tier_for_transition(prior_state, new_state) -> tuple[int, float]:
    if _is_upward(prior_state, new_state):
        return HYSTERESIS_TIERS.get(new_state, _DOWNWARD_TIER)
    return _DOWNWARD_TIER


def _find_settled_regime(history: list[dict]) -> str | None:
    """Most recent fired_redeploy row, else first row with streak>=3.
    Mirrors run_intraday_market_state.py:392-423 (fired_liquidation->fired_redeploy)."""
    for row in history:
        state = row.get('state')
        if not state:
            continue
        if row.get('fired_redeploy'):
            return state
        try:
            if int(row.get('hysteresis_streak') or 0) >= 3:
                return state
        except (TypeError, ValueError):
            pass
    return None


def confirmed_transition(history: list[dict], current_state: str, streak: int,
                         current_confidence: float) -> tuple[bool, str | None]:
    """Mirrors run_intraday_market_state.py:426-453. Returns (fired, prior_state)."""
    settled = _find_settled_regime(history)
    if settled is None or settled == current_state:
        return False, None
    n_required, conf_required = _tier_for_transition(settled, current_state)
    if streak < n_required or current_confidence < conf_required:
        return False, None
    return True, settled
