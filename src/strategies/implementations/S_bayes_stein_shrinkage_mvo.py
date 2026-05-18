"""
Bayes-Stein Shrinkage Mean-Variance Portfolio (Jorion 1986)

Shrinks sample mean return estimates toward a common prior (grand mean) via
empirical Bayes (Stein's inadmissibility result), then solves a long-only MVO.
Covariance matrix is kept at the sample estimate — only means are shrunk.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['BayesSteinShrinkageMVO']

STRATEGY_ID = 'S_bayes_stein_shrinkage_mvo'
LOOKBACK     = 756      # ~3 yr daily — satisfies min_lookback_required (504)
MAX_ASSETS   = 40       # cap: 756 / 40 = 18.9x > 3x obs-to-assets rule
LAMBDA       = 3.0      # risk-aversion λ in u(w) = w'μ - (λ/2)w'Σw
MIN_WEIGHT   = 0.005    # prune negligible allocations


class BayesSteinShrinkageMVO(BaseStrategy):
    """Bayes-Stein mean shrinkage toward grand prior; solve long-only MVO (Jorion 1986)."""

    id          = STRATEGY_ID
    name        = 'BayesSteinShrinkageMVO'
    description = (
        'Shrink sample mean return estimates toward grand mean prior via empirical '
        'Bayes (Jorion 1986 Stein result); solve long-only mean-variance portfolio.'
    )
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'lam': LAMBDA, 'max_assets': MAX_ASSETS}

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)
        lkbk  = int(self.parameters.get('lookback', LOOKBACK))
        lam   = float(self.parameters.get('lam', LAMBDA))
        max_n = int(self.parameters.get('max_assets', MAX_ASSETS))

        # ── 1. Data prep ────────────────────────────────────────────────
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        px = prices[tickers].tail(lkbk + 1).dropna(axis=1, how='any')
        if len(px) < lkbk:
            print(f'[debug] signals=0 (insufficient rows: {len(px)})', file=sys.stderr)
            return []

        # Enforce T >= 3*N to keep covariance well-conditioned
        n_cap = min(max_n, len(px.columns), len(px) // 3)
        if n_cap < 5:
            print(f'[debug] signals=0 (n_cap={n_cap} after obs/assets guard)', file=sys.stderr)
            return []

        rets = px.pct_change().dropna()
        # Prefer the most complete assets, then highest variance (liquidity proxy)
        completeness = rets.notna().mean()
        top_complete = completeness.nlargest(n_cap * 2).index.tolist()
        rets = rets[top_complete].dropna(axis=1)
        if rets.shape[1] < 5:
            print('[debug] signals=0 (too few complete assets)', file=sys.stderr)
            return []

        n_final = min(n_cap, rets.shape[1])
        keep    = rets.var().nlargest(n_final).index.tolist()
        rets    = rets[keep]
        T, N    = rets.shape

        # ── 2. Jorion 1986 Bayes-Stein shrinkage (means only) ───────────
        # mu_prior = equal-weighted grand mean
        mu_s      = rets.mean().values          # (N,) sample means
        mu_prior  = float(mu_s.mean())          # grand mean = shrinkage target

        # Sample covariance (used unmodified in MVO)
        sigma_s   = rets.cov().values           # (N, N)
        sigma_s   = (sigma_s + sigma_s.T) / 2 + np.eye(N) * 1e-8

        # Shrinkage weight: w = sigma2_est / (sigma2_cross + sigma2_est)
        # sigma2_est  = (1/T) * trace(Sigma) / N  — per-asset estimation uncertainty
        # sigma2_cross = cross-sectional variance of sample means
        sigma2_est   = float(np.trace(sigma_s)) / (T * N)
        sigma2_cross = float(np.var(mu_s, ddof=1)) if N > 1 else 1e-10
        if sigma2_cross < 1e-14:
            sigma2_cross = 1e-10
        w_shrink = sigma2_est / (sigma2_cross + sigma2_est)
        w_shrink = float(np.clip(w_shrink, 0.0, 1.0))

        # Shrunk means: mu_bs = (1-w)*mu_sample + w*mu_prior
        mu_bs = (1.0 - w_shrink) * mu_s + w_shrink * mu_prior

        # ── 3. Long-only MV optimisation ────────────────────────────────
        try:
            from scipy.optimize import minimize

            def neg_u(w):
                return -(w @ mu_bs - (lam / 2.0) * (w @ sigma_s @ w))

            def jac_neg_u(w):
                return -(mu_bs - lam * sigma_s @ w)

            res = minimize(
                neg_u, np.ones(N) / N, jac=jac_neg_u, method='SLSQP',
                bounds=[(0.0, 1.0)] * N,
                constraints=[{'type': 'eq', 'fun': lambda w: w.sum() - 1}],
                options={'ftol': 1e-9, 'maxiter': 500},
            )
            if res.success and np.all(np.isfinite(res.x)):
                weights = res.x
            else:
                var_s   = np.diag(sigma_s)
                weights = (1.0 / np.maximum(var_s, 1e-10))
                weights /= weights.sum()
        except Exception:
            var_s   = np.diag(sigma_s)
            weights = (1.0 / np.maximum(var_s, 1e-10))
            weights /= weights.sum()

        weights = np.maximum(weights, 0.0)
        total   = weights.sum()
        if total < 1e-10:
            print('[debug] signals=0 (degenerate weights)', file=sys.stderr)
            return []
        weights /= total

        # ── 4. Build signals ────────────────────────────────────────────
        active = sorted(
            [(keep[i], float(weights[i])) for i in range(N) if weights[i] >= MIN_WEIGHT],
            key=lambda x: -x[1],
        )[: self.MAX_SIGNALS]

        if not active:
            print('[debug] signals=0 (all weights below threshold)', file=sys.stderr)
            return []

        last_px = px[keep].iloc[-1]
        signals: List[Signal] = []
        for ticker, w in active:
            price = float(last_px.get(ticker, 0.0))
            if price <= 0.0 or not np.isfinite(price):
                continue
            atr_scale = REGIME_ATR_SCALE.get(regime_state, 1.0)
            atr = float(px[ticker].diff().abs().tail(20).mean()) * atr_scale
            if not np.isfinite(atr) or atr <= 0.0:
                atr = price * 0.02
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = round(price - 2.0 * atr, 4),
                target_1          = round(price + 1.5 * atr, 4),
                target_2          = round(price + 3.0 * atr, 4),
                target_3          = round(price + 4.5 * atr, 4),
                position_size_pct = float(w * scale),
                confidence        = 'HIGH' if w > 0.05 else ('MED' if w > 0.02 else 'LOW'),
                signal_params     = {
                    'w_shrink':  float(w_shrink),
                    'mu_prior':  float(mu_prior),
                    'mv_weight': float(w),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
