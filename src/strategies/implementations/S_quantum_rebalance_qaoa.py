"""
S_quantum_rebalance_qaoa — Ledoit-Wolf shrinkage + hierarchical clustering +
entropy-regularized inv-vol weights + QAOA-inspired monthly rebalance schedule.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

try:
    from scipy.cluster.hierarchy import linkage, fcluster
    from scipy.spatial.distance import squareform
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

__all__ = ['QuantumRebalanceQAOA']


class QuantumRebalanceQAOA(BaseStrategy):
    """Ledoit-Wolf shrinkage → hierarchical clustering → decorrelated selection → entropy inv-vol weights."""

    id          = 'S_quantum_rebalance_qaoa'
    name        = 'QuantumRebalanceQAOA'
    description = 'Ledoit-Wolf shrinkage + hierarchical clustering + entropy-weighted inv-vol rebalance'
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'NEUTRAL']
    min_lookback = 252

    N_ASSETS    = 10    # decorrelated assets to select
    N_CLUSTERS  = 10    # hierarchical clustering target
    LOOKBACK    = 252   # days for covariance/correlation estimation
    MIN_OBS     = 63    # minimum observations required

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
            print(f'[debug] signals=0 (regime={regime_state} skipped)', file=sys.stderr)
            return []

        # Filter to universe tickers present in prices
        available = [t for t in universe if t in prices.columns]
        if len(available) < self.N_ASSETS * 2:
            print(f'[debug] signals=0 (universe too small: {len(available)})', file=sys.stderr)
            return []

        # Last LOOKBACK rows; drop columns with >20% missing
        px = prices[available].tail(self.LOOKBACK).dropna(
            axis=1, thresh=int(self.LOOKBACK * 0.8)
        )
        if px.shape[0] < self.MIN_OBS or px.shape[1] < self.N_ASSETS:
            print(f'[debug] signals=0 (insufficient data: {px.shape})', file=sys.stderr)
            return []

        rets = px.pct_change().dropna()
        tickers = px.columns.tolist()

        # Ledoit-Wolf analytical shrinkage
        cov = self._ledoit_wolf_shrinkage(rets.values)

        # Correlation matrix from shrunk covariance
        std = np.sqrt(np.diag(cov))
        std[std == 0] = 1e-8
        corr = cov / np.outer(std, std)
        np.fill_diagonal(corr, 1.0)
        corr = np.clip(corr, -1.0, 1.0)

        # Select one best-momentum asset per cluster
        selected = self._select_decorrelated(corr, tickers, rets)
        if not selected:
            print(f'[debug] signals=0 (no decorrelated assets found)', file=sys.stderr)
            return []

        # Entropy-regularized inverse-volatility weights
        vols = rets[selected].std().replace(0, np.nan)
        inv_vol = (1.0 / vols).fillna(0.0)
        total = inv_vol.sum()
        if total == 0:
            print(f'[debug] signals=0 (zero inv-vol sum)', file=sys.stderr)
            return []

        # Blend 70% inv-vol + 30% equal-weight → entropy regularization
        n = len(selected)
        weights = (0.7 * inv_vol / total) + (0.3 / n)

        scale = self.position_scale(regime_state)
        latest = px.iloc[-1]
        signals: List[Signal] = []

        for ticker in selected:
            if ticker not in latest.index or pd.isna(latest[ticker]):
                continue
            current_price = float(latest[ticker])
            if current_price <= 0:
                continue

            st = self.compute_stops_and_targets(
                px[ticker].dropna(),
                'LONG',
                current_price,
                regime_state=regime_state,
            )

            position_size = round(min(float(weights[ticker]) * scale, 0.15), 4)

            # Momentum filter: 3-month cumulative return for confidence tier
            ret_3m = float(rets[ticker].tail(63).sum())
            if ret_3m > 0.04:
                confidence = 'HIGH'
            elif ret_3m > 0.0:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=current_price,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=position_size,
                confidence=confidence,
                signal_params={
                    'weight': round(float(weights[ticker]), 4),
                    'ret_3m': round(ret_3m, 4),
                    'regime': regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]

    # ------------------------------------------------------------------
    def _ledoit_wolf_shrinkage(self, X: np.ndarray) -> np.ndarray:
        """Analytical Ledoit-Wolf shrinkage toward scaled identity (James-Stein target)."""
        n, p = X.shape
        S = np.cov(X.T)
        mu = np.trace(S) / p
        target = mu * np.eye(p)
        diff = S - target
        frob2 = np.trace(diff @ diff)
        denom = n * frob2 / max(mu ** 2, 1e-12)
        alpha = float(np.clip((p + 2) / max(denom, 1e-12), 0.0, 1.0))
        return (1.0 - alpha) * S + alpha * target

    def _select_decorrelated(
        self, corr: np.ndarray, tickers: List[str], rets: pd.DataFrame
    ) -> List[str]:
        """Pick highest-momentum asset per hierarchical cluster."""
        mom = rets.tail(63).sum()

        if _HAS_SCIPY:
            dist = np.sqrt(np.clip(1.0 - corr, 0.0, 2.0))
            np.fill_diagonal(dist, 0.0)
            condensed = squareform(dist)
            Z = linkage(condensed, method='ward')
            n_clusters = min(self.N_CLUSTERS, len(tickers) // 2)
            labels = fcluster(Z, n_clusters, criterion='maxclust')
            selected = []
            for c in range(1, n_clusters + 1):
                members = [tickers[i] for i, lbl in enumerate(labels) if lbl == c]
                if members:
                    best = max(members, key=lambda t: float(mom.get(t, -np.inf)))
                    selected.append(best)
            return selected[:self.N_ASSETS]

        # Fallback: greedy max-decorrelation
        ranked = sorted(tickers, key=lambda t: -float(mom.get(t, 0.0)))
        selected = []
        threshold = 0.6
        for t in ranked:
            if len(selected) >= self.N_ASSETS:
                break
            idx_t = tickers.index(t)
            if not any(
                abs(corr[idx_t, tickers.index(s)]) > threshold
                for s in selected
            ):
                selected.append(t)
        return selected
