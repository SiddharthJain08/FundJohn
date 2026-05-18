"""
Empirical Bayes Shrinkage Mean-Variance Portfolio (Frost & Savarino 1986)

Shrinks each security's sample mean, variance, and pairwise correlations toward
their cross-sectional grand means via James-Stein-type empirical Bayes intensities,
then solves the long-only MV QP for portfolio weights.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['EmpiricalBayesShrinkageMV']

STRATEGY_ID = 'S_empirical_bayes_shrinkage_mv'
LOOKBACK     = 756      # ~3 yr daily — satisfies min_lookback_required
MAX_ASSETS   = 50       # cap: 756 / 50 = 15x > 3x obs-to-assets rule
LAMBDA       = 3.0      # risk-aversion λ in u(w) = w'μ - (λ/2)w'Σw
MIN_WEIGHT   = 0.005    # prune negligible allocations


class EmpiricalBayesShrinkageMV(BaseStrategy):
    """Shrink returns/variances/correlations to grand mean via EB, solve long-only MV QP."""

    id          = STRATEGY_ID
    name        = 'EmpiricalBayesShrinkageMV'
    description = (
        'Empirical Bayes shrinkage of sample returns, variances, and correlations '
        'toward cross-sectional grand means; solve long-only MV QP for portfolio '
        'weights (Frost & Savarino 1986).'
    )
    tier        = 2
    # All-weather: shrinkage improves estimation in any regime
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'lam': LAMBDA, 'max_assets': MAX_ASSETS}

    # ------------------------------------------------------------------ #
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
        scale  = self.position_scale(regime_state)
        lkbk   = int(self.parameters.get('lookback', LOOKBACK))
        lam    = float(self.parameters.get('lam', LAMBDA))
        max_n  = int(self.parameters.get('max_assets', MAX_ASSETS))

        # ── 1. Data prep ────────────────────────────────────────────────
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        px = prices[tickers].tail(lkbk + 1).dropna(axis=1, how='any')
        if len(px) < lkbk:
            print(f'[debug] signals=0 (insufficient rows: {len(px)})', file=sys.stderr)
            return []

        # Cap assets so T ≥ 3 N (avoids ill-conditioned covariance)
        n_cap = min(max_n, len(px.columns), len(px) // 3)
        if n_cap < 5:
            print(f'[debug] signals=0 (n_cap={n_cap} after obs/assets guard)', file=sys.stderr)
            return []

        rets = px.pct_change().dropna()
        # Select n_cap assets by completeness (fewest NaNs), then by volatility
        completeness = rets.notna().mean()
        top_complete  = completeness.nlargest(n_cap * 2).index.tolist()
        rets = rets[top_complete].dropna(axis=1)
        if rets.shape[1] < 5:
            print(f'[debug] signals=0 (too few complete assets)', file=sys.stderr)
            return []
        # Final asset selection: highest variance proxy for liquidity
        n_final = min(n_cap, rets.shape[1])
        keep    = rets.var().nlargest(n_final).index.tolist()
        rets    = rets[keep]
        T, N    = rets.shape

        # ── 2. Empirical Bayes shrinkage ────────────────────────────────
        mu_s     = rets.mean().values                    # (N,) sample means
        mu_g     = float(mu_s.mean())                    # grand mean
        var_s    = rets.var(ddof=1).values               # (N,)
        var_g    = float(var_s.mean())                   # grand variance
        corr_m   = rets.corr().values                    # (N, N)
        off_idx  = np.triu_indices(N, k=1)
        off_rho  = corr_m[off_idx]
        rho_g    = float(off_rho.mean()) if len(off_rho) else 0.0

        def _js_intensity(deviations, dof_num, T_):
            """James-Stein shrinkage intensity toward grand mean."""
            sq = float(np.sum(deviations ** 2))
            if sq < 1e-14:
                return 0.5
            return float(np.clip(dof_num / (T_ * sq), 0.0, 1.0))

        alpha = _js_intensity(mu_s - mu_g,        max(N - 2, 1), T)
        beta  = _js_intensity(var_s - var_g,       max(N - 1, 1), T)
        gamma = _js_intensity(off_rho - rho_g,     max(len(off_rho) - 1, 1), T)

        mu_hat  = (1 - alpha) * mu_s + alpha * mu_g
        var_hat = np.maximum((1 - beta) * var_s + beta * var_g, 1e-10)
        std_hat = np.sqrt(var_hat)
        rho_hat = np.clip((1 - gamma) * corr_m + gamma * rho_g, -0.999, 0.999)
        np.fill_diagonal(rho_hat, 1.0)

        # Shrunk covariance Σ̂ᵢⱼ = ρ̂ᵢⱼ σ̂ᵢ σ̂ⱼ
        sigma_hat = rho_hat * np.outer(std_hat, std_hat)
        sigma_hat = (sigma_hat + sigma_hat.T) / 2 + np.eye(N) * 1e-8

        # ── 3. Long-only MV QP ──────────────────────────────────────────
        try:
            from scipy.optimize import minimize

            def neg_u(w):
                return -(w @ mu_hat - (lam / 2) * (w @ sigma_hat @ w))

            def jac_neg_u(w):
                return -(mu_hat - lam * sigma_hat @ w)

            res = minimize(
                neg_u, np.ones(N) / N, jac=jac_neg_u, method='SLSQP',
                bounds=[(0.0, 1.0)] * N,
                constraints=[{'type': 'eq', 'fun': lambda w: w.sum() - 1}],
                options={'ftol': 1e-9, 'maxiter': 500},
            )
            weights = res.x if (res.success and np.all(np.isfinite(res.x))) else (1.0 / var_hat) / (1.0 / var_hat).sum()
        except Exception:
            weights = (1.0 / var_hat) / (1.0 / var_hat).sum()

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
                stop_loss         = price - 2.0 * atr,
                target_1          = price + 1.5 * atr,
                target_2          = price + 3.0 * atr,
                target_3          = price + 4.5 * atr,
                position_size_pct = float(w * scale),
                confidence        = 'HIGH' if w > 0.05 else ('MED' if w > 0.02 else 'LOW'),
                signal_params     = {
                    'alpha': float(alpha), 'beta': float(beta), 'gamma': float(gamma),
                    'mv_weight': float(w),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
