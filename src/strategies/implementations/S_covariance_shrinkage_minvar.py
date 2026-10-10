"""
Covariance Shrinkage: Why the Sample Matrix Lies at Scale
Paper: Melchor Alaiz, H. (2026) — https://hmaquant.substack.com/p/covariance-shrinkage-why-the-sample

Sample covariance matrices estimated from short windows badly distort
minimum-variance portfolio weights. Shrinking the sample covariance ~40%
toward a simple target matrix recovers most of the true weighting:
    Sigma_shrunk = (1 - delta) * Sigma_sample + delta * Sigma_target,  delta ~= 0.4
    w_minvar     = (Sigma_shrunk^-1 * 1) / (1' * Sigma_shrunk^-1 * 1)

Variant 1 of 2 (vs. Ledoit-Wolf analytic-rho / identity-scaled target /
log-return / HRP-blended siblings already in this fleet,
S_schur_damped_minvar_shrinkage and S_nco_schur_bridge_minvar): fixed
delta=0.40 taken literally from the paper (no data-driven rho estimation),
a constant-correlation target matrix (Elton-Gruber style: off-diagonals
replaced by the universe's average pairwise correlation, diagonal keeps
sample variances), simple (not log) returns, and a plain long-only
min-variance solve with no clustering/blending overlay.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['CovarianceShrinkageMinVar']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_covariance_shrinkage_minvar'
LOOKBACK = 504        # spec's min_lookback_required (~2 trading years)
MAX_ASSETS = 60        # T_eff/N ~= 503/60 ~= 8.4x, well above the 3x safety floor
MIN_UNIVERSE = 50      # spec's minimum_universe_size
DELTA = 0.40           # spec's reported shrinkage intensity, taken literally
MIN_WEIGHT = 5e-4      # ignore dust allocations


def _constant_corr_target(sigma: np.ndarray) -> np.ndarray:
    """Elton-Gruber constant-correlation target: off-diagonals replaced by
    the universe's average pairwise correlation; diagonal keeps sample
    variances untouched (only the correlation structure is shrunk)."""
    n = sigma.shape[0]
    std = np.sqrt(np.maximum(np.diag(sigma), 1e-14))
    corr = sigma / np.outer(std, std)
    off_mask = ~np.eye(n, dtype=bool)
    rho_bar = float(np.mean(corr[off_mask])) if n > 1 else 0.0
    target_corr = np.full((n, n), rho_bar)
    np.fill_diagonal(target_corr, 1.0)
    return target_corr * np.outer(std, std)


def _minvar_weights(sigma: np.ndarray) -> np.ndarray:
    """Analytical global minimum-variance weights, long-only via clipping."""
    n = sigma.shape[0]
    try:
        inv_s = np.linalg.pinv(sigma)
        ones = np.ones(n)
        w = inv_s @ ones
        denom = float(ones @ inv_s @ ones)
        w = w / denom if abs(denom) > 1e-12 else np.ones(n) / n
    except Exception:
        w = np.ones(n) / n
    w = np.maximum(w, 0.0)
    total = w.sum()
    return w / total if total > 1e-10 else np.ones(n) / n


class CovarianceShrinkageMinVar(BaseStrategy):
    """Fixed-intensity constant-correlation shrinkage toward a stable
    long-only minimum-variance equity portfolio."""

    id                = STRATEGY_ID
    name              = 'CovarianceShrinkageMinVar'
    description       = ('Shrinks the sample covariance 40% toward a constant-correlation '
                         'target and solves for long-only global minimum-variance weights.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'max_assets': MAX_ASSETS, 'delta': DELTA}

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)
        lookback = int(self.parameters.get('lookback', LOOKBACK))
        max_assets = int(self.parameters.get('max_assets', MAX_ASSETS))
        delta = float(self.parameters.get('delta', DELTA))

        cols = [t for t in universe if t in prices.columns]
        if len(cols) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (universe {len(cols)} < min {MIN_UNIVERSE})', file=sys.stderr)
            return []

        sub = prices[cols].dropna(axis=1, thresh=lookback)
        if sub.shape[0] < lookback or sub.shape[1] < MIN_UNIVERSE:
            print(f'[debug] signals=0 (shape {sub.shape} < lookback={lookback})', file=sys.stderr)
            return []
        sub = sub.iloc[-lookback:]

        if sub.shape[1] > max_assets:
            sub = sub[sub.isna().sum().nsmallest(max_assets).index]
        sub = sub.dropna()

        tickers = list(sub.columns)
        N = len(tickers)
        T_eff = sub.shape[0] - 1   # rows after differencing
        if T_eff < 3 * N or N < MIN_UNIVERSE:
            print(f'[debug] signals=0 (underdetermined T={T_eff} N={N})', file=sys.stderr)
            return []

        returns = sub.pct_change().dropna().values
        sigma_sample = np.cov(returns.T)
        sigma_target = _constant_corr_target(sigma_sample)
        sigma_shrunk = (1.0 - delta) * sigma_sample + delta * sigma_target

        w = _minvar_weights(sigma_shrunk)
        if w.sum() < 1e-10:
            print('[debug] signals=0 (zero minvar weights)', file=sys.stderr)
            return []

        last = sub.iloc[-1]
        signals: List[Signal] = []
        for i in np.argsort(w)[::-1]:
            if len(signals) >= self.MAX_SIGNALS:
                break
            wi = float(w[i])
            if wi < MIN_WEIGHT:
                continue
            ticker = tickers[i]
            price = float(last[ticker])
            if price <= 0:
                continue
            st = self.compute_stops_and_targets(sub[ticker], 'LONG', price, regime_state=regime_state)
            pos = round(wi * scale, 4)
            conf = 'HIGH' if wi > 0.05 else ('MED' if wi > 0.02 else 'LOW')
            signals.append(Signal(
                ticker=ticker, direction='LONG',
                entry_price=price,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=pos,
                confidence=conf,
                signal_params={
                    'delta':  round(delta, 4),
                    'weight': round(wi, 6),
                },
            ))

        print(f'[debug] signals={len(signals)} delta={delta:.2f} N={N}', file=sys.stderr)
        return signals
