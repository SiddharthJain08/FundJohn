"""
Nested-Clustered / Schur-Bridge Minimum-Variance Portfolio. Paper: "Nested
Clustered Optimization Is One End of a Schur Bridge, and the Interior Is
Sometimes Provably Better" -- Cotton (2026) arXiv:2609.21271

Clusters the universe; within each cluster, a min-variance leg is computed
on a gamma-damped cross-cluster Schur complement (gamma=0 -> block-
diagonal/pure NCO, gamma=1 -> full conditioning); clusters are then
overlaid by inverse realized variance. gamma is a closed-form estimation-
error proxy (the paper reports a stylized closed-form example, not a
fitting procedure) -- not a numeric/backtest search.

Variant 1 of 2 (vs. sqrt(N)-adaptive clusters / Ward+bisection / grid-
search gamma / long-only projection): fixed cluster count, average-linkage
flat cut, analytic gamma*, signed long/short weights.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['NcoSchurBridgeMinVar']
INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_nco_schur_bridge_minvar'
LOOKBACK = 756       # paper's own min_lookback_required
MAX_ASSETS = 90      # T_eff/N >= 3x holds (LOOKBACK/3 = 252 >> 90)
N_CLUSTERS = 5        # fixed cluster count (variant 1 choice)
MIN_UNIVERSE = 50     # spec minimum_universe_size
MIN_WEIGHT = 5e-4

def _shrunk_cov(X: np.ndarray) -> np.ndarray:
    """Ledoit-Wolf shrunk covariance -- keeps block inversions well-posed."""
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

def _cluster_labels(sigma: np.ndarray, k: int) -> np.ndarray:
    """Flat correlation-distance clusters: average-linkage + maxclust cut."""
    N = sigma.shape[0]
    std = np.sqrt(np.maximum(np.diag(sigma), 1e-12))
    corr = np.clip(sigma / np.outer(std, std), -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    dist = np.sqrt(np.maximum(0.5 * (1.0 - corr), 0.0))
    try:
        from scipy.cluster.hierarchy import linkage, fcluster
        from scipy.spatial.distance import squareform
        Z = linkage(squareform(dist, checks=False), method='average')
        return fcluster(Z, t=k, criterion='maxclust')
    except Exception:
        return (np.arange(N) % max(k, 1)) + 1  # deterministic round-robin fallback

def _closed_form_gamma(T_eff: int, N: int, K: int) -> float:
    """Analytic (no CV/grid-search) damping dial: T_eff>>N -> 1 (trust full
    conditioning); T_eff~N -> 0 (truncate to block-diagonal NCO)."""
    denom = T_eff + N - K
    return float(np.clip((T_eff - K) / denom, 0.0, 1.0)) if denom > 0 else 0.0

def _schur_bridge_weights(sigma: np.ndarray, labels: np.ndarray, gamma: float) -> np.ndarray:
    """Per-cluster min-variance leg on a gamma-damped Schur complement, then
    an inverse-realized-variance overlay across clusters."""
    N = sigma.shape[0]
    w = np.zeros(N)
    clusters = np.unique(labels)
    cluster_var, cluster_w = {}, {}
    for c in clusters:
        idx_c = np.where(labels == c)[0]
        idx_o = np.where(labels != c)[0]
        sigma_cc = sigma[np.ix_(idx_c, idx_c)]
        if idx_o.size == 0:
            d_c = sigma_cc
        else:
            sigma_co = sigma[np.ix_(idx_c, idx_o)]
            sigma_oo = sigma[np.ix_(idx_o, idx_o)]
            cond = sigma_co @ np.linalg.pinv(sigma_oo) @ sigma_co.T
            d_c = sigma_cc - gamma * cond   # 0: block-diag NCO; 1: full Schur complement
        try:
            v_c = np.linalg.pinv(d_c) @ np.ones(idx_c.size)
        except Exception:
            v_c = np.ones(idx_c.size)
        norm = np.sum(np.abs(v_c))
        v_c = v_c / norm if norm > 1e-10 else np.ones(idx_c.size) / idx_c.size
        cluster_var[c] = max(float(v_c @ sigma_cc @ v_c), 1e-12)  # realized on UNdamped block
        cluster_w[c] = (idx_c, v_c)
    inv_var = {c: 1.0 / cluster_var[c] for c in clusters}
    tot = sum(inv_var.values())
    for c in clusters:
        idx_c, v_c = cluster_w[c]
        u_c = inv_var[c] / tot if tot > 1e-10 else 1.0 / len(clusters)
        w[idx_c] = u_c * v_c
    return w

class NcoSchurBridgeMinVar(BaseStrategy):
    """Damps each cluster's cross-cluster Schur complement by a closed-form
    gamma, bridging pure NCO (block-diagonal) and full min-variance."""

    id                = STRATEGY_ID
    name              = 'NcoSchurBridgeMinVar'
    description       = ('Nested-clustered min-variance with a gamma-damped Schur-complement '
                         'bridge between block-diagonal NCO and full min-variance coupling.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'max_assets': MAX_ASSETS, 'n_clusters': N_CLUSTERS}

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
        n_clusters = int(self.parameters.get('n_clusters', N_CLUSTERS))

        cols = [t for t in universe if t in prices.columns]
        if len(cols) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (universe {len(cols)} < min {MIN_UNIVERSE})', file=sys.stderr)
            return []

        sub = prices[cols].dropna(axis=1, thresh=lookback)
        if sub.shape[0] < lookback or sub.shape[1] < 2 * n_clusters:
            print(f'[debug] signals=0 (shape {sub.shape} < lookback={lookback})', file=sys.stderr)
            return []
        sub = sub.iloc[-lookback:]
        if sub.shape[1] > max_assets:
            sub = sub[sub.isna().sum().nsmallest(max_assets).index]
        sub = sub.dropna()

        N = sub.shape[1]
        T_eff = sub.shape[0] - 1
        n_clusters_eff = max(2, min(n_clusters, N // 5))
        if T_eff < 3 * N or N < 2 * n_clusters_eff:
            print(f'[debug] signals=0 (underdetermined T={T_eff} N={N})', file=sys.stderr)
            return []

        tickers = list(sub.columns)
        log_ret = np.log(sub / sub.shift(1)).dropna().values
        sigma = _shrunk_cov(log_ret)
        labels = _cluster_labels(sigma, n_clusters_eff)
        k_actual = len(np.unique(labels))
        gamma = _closed_form_gamma(T_eff, N, k_actual)
        w = _schur_bridge_weights(sigma, labels, gamma)

        gross = np.sum(np.abs(w))
        if gross < 1e-10:
            print('[debug] signals=0 (zero bridge weights)', file=sys.stderr)
            return []
        w = w / gross

        last = sub.iloc[-1]
        signals: List[Signal] = []
        for i in np.argsort(-np.abs(w)):
            if len(signals) >= self.MAX_SIGNALS:
                break
            wi = float(w[i])
            if abs(wi) < MIN_WEIGHT:
                continue
            ticker = tickers[i]
            price = float(last[ticker])
            if price <= 0:
                continue
            direction = 'LONG' if wi >= 0 else 'SHORT'
            st = self.compute_stops_and_targets(sub[ticker], direction, price, regime_state=regime_state)
            pos = round(abs(wi) * scale, 4)
            conf = 'HIGH' if abs(wi) > 0.05 else ('MED' if abs(wi) > 0.02 else 'LOW')
            signals.append(Signal(
                ticker=ticker, direction=direction, entry_price=price,
                stop_loss=float(st['stop']), target_1=float(st['t1']),
                target_2=float(st['t2']), target_3=float(st['t3']),
                position_size_pct=pos, confidence=conf,
                signal_params={
                    'gamma': round(gamma, 4), 'weight': round(wi, 6),
                    'cluster': int(labels[i]), 'n_clusters': int(k_actual),
                },
            ))

        print(f'[debug] signals={len(signals)} gamma={gamma:.4f} K={k_actual} N={N}', file=sys.stderr)
        return signals
