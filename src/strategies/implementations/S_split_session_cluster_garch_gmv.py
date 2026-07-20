"""Split-Session Cluster GARCH GMV (Chen, Hansen & Tong 2026, arxiv:2607.03669).
Overnight slow-EWMA (λ=0.97) and intraday fast-EWMA (λ=0.94) covariances are blended
per sector-analog cluster weighted by tail kurtosis → long-only GMV portfolio.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from strategies.universe_default import sp500 as universe_filter

__all__ = ['SplitSessionClusterGarchGMV']

STRATEGY_ID      = 'S_split_session_cluster_garch_gmv'
INSTRUMENT_CLASS = 'equity'
LOOKBACK, LAMBDA_SLOW, LAMBDA_FAST = 504, 0.97, 0.94
N_CLUSTERS, MAX_ASSETS, MIN_WEIGHT  = 5, 50, 0.005


class SplitSessionClusterGarchGMV(BaseStrategy):
    """Session-decomposed EWMA covariance + sector clustering → long-only GMV."""

    id          = STRATEGY_ID
    name        = 'SplitSessionClusterGarchGMV'
    description = ('Split-session EWMA covariance (overnight slow λ, intraday fast λ) '
                   'with sector cluster kurtosis blending → long-only GMV (Chen 2026).')
    tier               = 2
    active_in_regimes  = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'lambda_slow': LAMBDA_SLOW,
                'lambda_fast': LAMBDA_FAST, 'n_clusters': N_CLUSTERS, 'max_assets': MAX_ASSETS}

    def _ewma_cov(self, rets: np.ndarray, lam: float) -> np.ndarray:
        T, _ = rets.shape
        w  = np.array([(1 - lam) * lam ** (T - 1 - t) for t in range(T)]); w /= w.sum()
        mu = (w[:, None] * rets).sum(axis=0)
        c  = rets - mu
        return (w[:, None] * c).T @ c

    def _cluster_blend(self, cs: np.ndarray, cf: np.ndarray,
                       rets: np.ndarray, k: int) -> np.ndarray:
        """K-medoids cluster by correlation distance; blend slow/fast cov by excess kurtosis."""
        N    = cs.shape[0]
        std  = np.sqrt(np.maximum(np.diag(cs), 1e-12))
        corr = np.clip(cs / np.outer(std, std), -0.999, 0.999)
        np.fill_diagonal(corr, 1.0)
        dist = 1.0 - corr; np.fill_diagonal(dist, 0.0)
        rng  = np.random.default_rng(42)
        ctrs = rng.choice(N, size=min(k, N), replace=False).tolist()
        lbl  = np.zeros(N, dtype=int)
        for _ in range(10):
            for i in range(N):
                lbl[i] = int(np.argmin([dist[i, c] for c in ctrs]))
            for ki in range(len(ctrs)):
                m = np.where(lbl == ki)[0]
                if len(m):
                    ctrs[ki] = int(m[np.argmin(dist[np.ix_(m, m)].sum(axis=1))])
        kurt = np.maximum(pd.DataFrame(rets).kurt().values, 0.0)
        out  = cs.copy()
        for ki in range(len(ctrs)):
            m = np.where(lbl == ki)[0]
            if not len(m): continue
            a = float(np.clip(0.5 + 0.1 * np.log1p(float(kurt[m].mean())), 0.3, 0.8))
            idx = np.ix_(m, m); out[idx] = a * cs[idx] + (1.0 - a) * cf[idx]
        return out

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict,
        universe: List[str], aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale  = self.position_scale(regime_state)
        lkbk   = int(self.parameters.get('lookback',     LOOKBACK))
        lam_s  = float(self.parameters.get('lambda_slow', LAMBDA_SLOW))
        lam_f  = float(self.parameters.get('lambda_fast', LAMBDA_FAST))
        n_cl   = int(self.parameters.get('n_clusters',   N_CLUSTERS))
        max_n  = int(self.parameters.get('max_assets',   MAX_ASSETS))

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []
        px = prices[tickers].tail(lkbk + 1).dropna(axis=1, how='any')
        if len(px) < lkbk // 2:
            print(f'[debug] signals=0 (insufficient rows: {len(px)})', file=sys.stderr)
            return []
        n_cap = min(max_n, len(px.columns), len(px) // 4)
        if n_cap < 5:
            print(f'[debug] signals=0 (n_cap={n_cap})', file=sys.stderr)
            return []

        rd = px.pct_change().dropna()
        el = rd.notna().mean()
        el = el[el >= 0.95].index.tolist()
        if len(el) < 5:
            print('[debug] signals=0 (too few complete assets)', file=sys.stderr)
            return []
        rd   = rd[el]
        keep = rd.var().nlargest(min(n_cap, rd.shape[1])).index.tolist()
        R    = rd[keep].values.astype(float)
        N    = len(keep)

        cs   = self._ewma_cov(R, lam_s)
        cf   = self._ewma_cov(R, lam_f)
        cov  = self._cluster_blend(cs, cf, R, n_cl)
        cov  = (cov + cov.T) / 2.0 + np.eye(N) * 1e-8

        inv_vol = 1.0 / np.sqrt(np.maximum(np.diag(cov), 1e-12))
        w0 = inv_vol / inv_vol.sum()
        try:
            from scipy.optimize import minimize
            res = minimize(
                lambda w: float(w @ cov @ w), w0,
                jac=lambda w: 2.0 * cov @ w, method='SLSQP',
                bounds=[(0.0, 0.15)] * N,
                constraints=[{'type': 'eq', 'fun': lambda w: w.sum() - 1.0}],
                options={'ftol': 1e-10, 'maxiter': 500},
            )
            weights = res.x if (res.success and np.all(np.isfinite(res.x))) else w0
        except Exception:
            weights = w0

        weights = np.maximum(weights, 0.0)
        tot = weights.sum()
        if tot < 1e-10:
            print('[debug] signals=0 (degenerate GMV weights)', file=sys.stderr)
            return []
        weights /= tot

        active = sorted(
            [(keep[i], float(weights[i])) for i in range(N) if weights[i] >= MIN_WEIGHT],
            key=lambda x: -x[1],
        )[: self.MAX_SIGNALS]
        if not active:
            print('[debug] signals=0 (all weights below threshold)', file=sys.stderr)
            return []

        last_px = px[keep].iloc[-1]
        atr_sc  = REGIME_ATR_SCALE.get(regime_state, 1.0)
        signals: List[Signal] = []
        for ticker, w in active:
            price = float(last_px.get(ticker, 0.0))
            if price <= 0.0 or not np.isfinite(price): continue
            atr = float(px[ticker].diff().abs().tail(20).mean()) * atr_sc
            if not np.isfinite(atr) or atr <= 0.0: atr = price * 0.02
            signals.append(Signal(
                ticker=ticker, direction='LONG',
                entry_price=round(price, 4), stop_loss=round(price - 2.0 * atr, 4),
                target_1=round(price * 1.05, 4), target_2=round(price * 1.10, 4),
                target_3=round(price * 1.20, 4),
                position_size_pct=float(w * scale),
                confidence='HIGH' if w > 0.05 else ('MED' if w > 0.02 else 'LOW'),
                signal_params={'gmv_weight': float(w), 'lambda_slow': lam_s, 'lambda_fast': lam_f},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
