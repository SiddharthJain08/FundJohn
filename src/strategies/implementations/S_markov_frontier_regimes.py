from __future__ import annotations
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['MarkovFrontierRegimes']


class MarkovFrontierRegimes(BaseStrategy):
    """Monthly Markov frontier regimes: cluster EF parabola coefficients, regime-weighted tangency E[w]."""

    id           = 'S_markov_frontier_regimes'
    name         = 'MarkovFrontierRegimes'
    description  = 'Monthly Markov frontier: cluster EF parabola coefficients, regime-weighted tangency E[w]'
    tier         = 2
    min_lookback = 504   # ~2 years of trading days

    N_REGIMES  = 3
    MIN_MONTHS = 12
    MAX_ASSETS = 20
    TOP_N      = 10

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

        tickers = [t for t in (universe if isinstance(universe, list) else list(universe))
                   if t in prices.columns and prices[t].notna().sum() >= 252][: self.MAX_ASSETS]
        if len(tickers) < 5:
            return []

        # ffill before resampling so monthly end-of-month price is never NaN due to gaps
        monthly = prices[tickers].ffill().resample('ME').last().pct_change().dropna(how='all')
        # Drop tickers that are mostly NaN after resampling (e.g. delisted mid-window)
        monthly = monthly.loc[:, monthly.notna().mean() >= 0.6]
        if monthly.shape[1] < 5:
            return []
        monthly = monthly.fillna(monthly.median())  # fill residual gaps with cross-sectional median

        if len(monthly) < self.MIN_MONTHS:
            return []

        coeffs = self._frontier_coefficients(monthly)
        if coeffs is None or len(coeffs) < 8:
            return []

        labels = self._cluster(coeffs)
        if labels is None:
            return []

        trans = self._transition_matrix(labels)
        cur   = int(labels[-1])
        prob  = trans[cur]

        # Soft-weighted tangency: use ALL monthly obs weighted by regime membership.
        # labels has length (len(monthly) - start_offset) due to rolling window;
        # pad the early months with soft weight 0.15 so shapes align.
        label_start = len(monthly) - len(labels)  # months before rolling window starts
        tang_weights: dict = {}
        for s in range(self.N_REGIMES):
            # Build full-length weight vector: early months get soft weight, labeled months get hard/soft
            full_weights = np.full(len(monthly), 0.15)
            full_weights[label_start:] = np.where(labels == s, 1.0, 0.15)
            w = self._tangency_weighted(monthly, full_weights)
            if w is not None:
                tang_weights[s] = w

        if not tang_weights:
            return []

        exp_w = pd.Series(0.0, index=tickers)
        for s, w in tang_weights.items():
            exp_w = exp_w.add(w * float(prob[s]), fill_value=0.0)

        exp_w = exp_w.clip(lower=0)
        if exp_w.sum() < 1e-8:
            return []
        exp_w /= exp_w.sum()

        last_px = prices[tickers].iloc[-1]
        signals: List[Signal] = []
        for ticker in exp_w.nlargest(self.TOP_N).index:
            wt    = float(exp_w[ticker])
            if wt < 0.01:
                continue
            price = float(last_px.get(ticker, 0))
            if price <= 0:
                continue
            st   = self.compute_stops_and_targets(
                prices[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            conf = 'HIGH' if wt > 0.10 else ('MED' if wt > 0.05 else 'LOW')
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=round(price, 4),
                stop_loss=st['stop'],
                target_1=st['t1'],
                target_2=st['t2'],
                target_3=st['t3'],
                position_size_pct=float(min(wt * scale, 0.10)),
                confidence=conf,
                signal_params={'exp_weight': round(wt, 4), 'cur_regime': cur},
            ))
        return signals[: self.MAX_SIGNALS]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _frontier_coefficients(self, monthly: pd.DataFrame) -> np.ndarray | None:
        """Analytic EF parabola coefficients [C/D, -2B/D, A/D] per rolling window."""
        rows = []
        n    = len(monthly)
        for t in range(12, n):
            ret = monthly.iloc[:t].values
            if np.any(np.isnan(ret)):
                continue
            mu = ret.mean(axis=0)
            try:
                cov  = np.cov(ret.T) + np.eye(ret.shape[1]) * 1e-6
                cinv = np.linalg.inv(cov)
                ones = np.ones(len(mu))
                A    = float(mu @ cinv @ mu)
                B    = float(mu @ cinv @ ones)
                C    = float(ones @ cinv @ ones)
                D    = A * C - B * B
                if abs(D) < 1e-12:
                    continue
                rows.append([C / D, -2.0 * B / D, A / D])
            except np.linalg.LinAlgError:
                continue
        return np.array(rows) if len(rows) >= 5 else None

    def _cluster(self, coeffs: np.ndarray) -> np.ndarray | None:
        from scipy.cluster.hierarchy import linkage, fcluster
        if len(coeffs) < self.N_REGIMES:
            return None
        try:
            std    = coeffs.std(axis=0)
            std[std < 1e-10] = 1.0
            normed = (coeffs - coeffs.mean(axis=0)) / std
            Z      = linkage(normed, method='ward')
            labels = fcluster(Z, self.N_REGIMES, criterion='maxclust') - 1
            return labels.astype(int)
        except Exception:
            return None

    def _transition_matrix(self, labels: np.ndarray) -> np.ndarray:
        n     = self.N_REGIMES
        trans = np.ones((n, n))   # Laplace smoothing
        for t in range(len(labels) - 1):
            s, e = int(labels[t]), int(labels[t + 1])
            if 0 <= s < n and 0 <= e < n:
                trans[s, e] += 1
        return trans / trans.sum(axis=1, keepdims=True)

    def _tangency_weighted(self, ret_df: pd.DataFrame, weights: np.ndarray) -> pd.Series | None:
        """
        Weighted tangency portfolio using all monthly observations.
        weights[t] = 1.0 for regime members, 0.15 for others — gives the optimizer
        the full history while emphasising the target regime's months.
        Uses Ledoit-Wolf shrinkage covariance for robustness with small samples.
        """
        from scipy.optimize import minimize
        try:
            from sklearn.covariance import LedoitWolf
        except ImportError:
            return None

        n_obs, n_assets = ret_df.shape
        if n_assets < 2 or n_obs < n_assets + 2:
            return None

        # Normalise weights so they sum to n_obs (preserves scale)
        w_norm = weights / weights.sum() * n_obs
        X = ret_df.values

        # Weighted mean
        mu = (X * w_norm[:, None]).sum(axis=0) / w_norm.sum()

        # Weighted Ledoit-Wolf covariance: demean then weight each row
        X_c = X - mu
        X_w = X_c * np.sqrt(w_norm[:, None])
        try:
            lw = LedoitWolf().fit(X_w)
            cov = lw.covariance_ + np.eye(n_assets) * 1e-8
        except Exception:
            cov = np.cov(X_w.T) + np.eye(n_assets) * 1e-6

        def neg_sharpe(p: np.ndarray) -> float:
            r = float(p @ mu)
            v = float(np.sqrt(p @ cov @ p + 1e-12))
            return -r / v if v > 0 else 0.0

        res = minimize(
            neg_sharpe, np.ones(n_assets) / n_assets, method='SLSQP',
            bounds=[(0, 1)] * n_assets,
            constraints=[{'type': 'eq', 'fun': lambda p: p.sum() - 1.0}],
            options={'ftol': 1e-9, 'maxiter': 500},
        )
        if not res.success:
            # Fallback: equal-weight among positive-mu assets
            pos = mu > 0
            if not pos.any():
                return None
            fallback = np.zeros(n_assets)
            fallback[pos] = 1.0 / pos.sum()
            return pd.Series(fallback, index=ret_df.columns)
        return pd.Series(res.x, index=ret_df.columns)
