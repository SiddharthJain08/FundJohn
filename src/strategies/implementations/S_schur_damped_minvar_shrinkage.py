"""
Schur-Damped Minimum-Variance Shrinkage Portfolio
Paper: "Two Sides of Schur Damping: High-Dimensional Pseudo-Likelihoods
       and Portfolio Allocation" — Cotton (2026) arXiv:2606.14798

Closed-form Ledoit-Wolf/James-Stein shrinkage intensity derived from the
Schur complement of the return covariance matrix optimally interpolates
between global minimum-variance (MV) and hierarchical risk parity (HRP),
yielding a more stable long-only portfolio than either endpoint alone.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['SchurDampedMinVarShrinkage']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_schur_damped_minvar_shrinkage'
LOOKBACK = 504      # ~2 trading years; paper minimum
MAX_ASSETS = 80     # keeps T/N ≥ 6× (satisfies 3× cov-invertibility safety)
MIN_WEIGHT = 5e-4   # ignore dust allocations


def _lw_cov(X: np.ndarray) -> np.ndarray:
    """Ledoit-Wolf shrunk covariance (sklearn preferred, analytic fallback)."""
    try:
        from sklearn.covariance import LedoitWolf
        return LedoitWolf().fit(X).covariance_
    except Exception:
        T, N = X.shape
        S = np.cov(X.T)
        mu = np.trace(S) / N
        delta = S - mu * np.eye(N)
        num = np.sum(delta ** 2)
        den = np.sum(S ** 2) - np.trace(S) ** 2 / N + 1e-14
        rho = min(1.0, ((N + 2) / 6) * num / (den * T))
        return (1 - rho) * S + rho * mu * np.eye(N)


def _mv_weights(sigma: np.ndarray) -> np.ndarray:
    """Analytical global-MV weights with long-only projection via clipping."""
    N = sigma.shape[0]
    try:
        inv_s = np.linalg.pinv(sigma)
        w = inv_s @ np.ones(N)
        w = np.maximum(w, 0.0)
        s = w.sum()
        return w / s if s > 1e-10 else np.ones(N) / N
    except Exception:
        return np.ones(N) / N


def _hrp_weights(sigma: np.ndarray) -> np.ndarray:
    """HRP: recursive bisection on Ward dendrogram using inverse-vol weights."""
    N = sigma.shape[0]
    std = np.sqrt(np.maximum(np.diag(sigma), 1e-12))
    corr = np.clip(sigma / np.outer(std, std), -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    dist = np.sqrt(np.maximum(0.5 * (1.0 - corr), 0.0))
    try:
        from scipy.cluster.hierarchy import linkage, to_tree
        from scipy.spatial.distance import squareform
        Z = linkage(squareform(dist, checks=False), method='ward')
        root, _ = to_tree(Z, rd=True)

        def _order(node):
            return [node.id] if node.is_leaf() else _order(node.left) + _order(node.right)

        def _bisect(ids):
            if len(ids) == 1:
                return np.array([1.0])
            m = len(ids) // 2
            wL, wR = _bisect(ids[:m]), _bisect(ids[m:])
            vL = float(wL @ sigma[np.ix_(ids[:m], ids[:m])] @ wL)
            vR = float(wR @ sigma[np.ix_(ids[m:], ids[m:])] @ wR)
            tot = vL + vR + 1e-14
            out = np.empty(len(ids))
            out[:m] = (vR / tot) * wL
            out[m:] = (vL / tot) * wR
            return out

        order = _order(root)
        w_ord = _bisect(order)
        w = np.zeros(N)
        for j, idx in enumerate(order):
            w[idx] = w_ord[j]
        w = np.maximum(w, 0.0)
        s = w.sum()
        return w / s if s > 1e-10 else (1.0 / std) / (1.0 / std).sum()
    except Exception:
        inv_v = 1.0 / std
        return inv_v / inv_v.sum()


class SchurDampedMinVarShrinkage(BaseStrategy):
    """Schur-complement damping interpolates MV and HRP for a stable equity portfolio."""

    id                = STRATEGY_ID
    name              = 'SchurDampedMinVarShrinkage'
    description       = ('Closed-form Schur/Ledoit-Wolf damping intensity blends '
                         'min-variance and HRP weights for a robust long-only equity portfolio.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'max_assets': MAX_ASSETS}

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

        cols = [t for t in universe if t in prices.columns]
        if not cols:
            print('[debug] signals=0 (no universe tickers in prices)', file=sys.stderr)
            return []

        sub = prices[cols].dropna(axis=1, thresh=lookback)
        if sub.shape[0] < lookback or sub.shape[1] < 10:
            print(f'[debug] signals=0 (shape {sub.shape} < lookback={lookback})', file=sys.stderr)
            return []
        sub = sub.iloc[-lookback:]

        # Select most-complete series up to max_assets
        if sub.shape[1] > max_assets:
            sub = sub[sub.isna().sum().nsmallest(max_assets).index]

        sub = sub.dropna()
        N = sub.shape[1]
        T_eff = sub.shape[0] - 1       # rows after log-differencing
        if T_eff < 3 * N or N < 5:
            print(f'[debug] signals=0 (underdetermined T={T_eff} N={N})', file=sys.stderr)
            return []

        tickers = list(sub.columns)
        log_ret = np.log(sub / sub.shift(1)).dropna().values

        sigma = _lw_cov(log_ret)

        # Schur complement: S_i = 1/(Σ⁻¹)_{ii}  (exact Schur identity)
        try:
            inv_s = np.linalg.pinv(sigma)
            schur = 1.0 / np.maximum(np.diag(inv_s), 1e-14)
        except Exception:
            schur = np.diag(sigma)

        diag_var = np.diag(sigma)
        lambda_star = float(np.clip(np.sum(schur ** 2) / (np.sum(diag_var ** 2) + 1e-14), 0.0, 1.0))

        w_mv  = _mv_weights(sigma)
        w_hrp = _hrp_weights(sigma)
        w = np.maximum((1.0 - lambda_star) * w_mv + lambda_star * w_hrp, 0.0)
        total = w.sum()
        if total < 1e-10:
            print('[debug] signals=0 (zero blend weights)', file=sys.stderr)
            return []
        w /= total

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
                    'lambda_star': round(lambda_star, 4),
                    'weight':      round(wi, 6),
                    'w_mv':        round(float(w_mv[i]), 6),
                    'w_hrp':       round(float(w_hrp[i]), 6),
                },
            ))

        print(f'[debug] signals={len(signals)} lambda*={lambda_star:.4f} N={N}', file=sys.stderr)
        return signals
