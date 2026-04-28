from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from scipy.optimize import minimize
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['PartialCointegrationPairs']


class PartialCointegrationPairs(BaseStrategy):
    """Pairs trading via partial cointegration: Kalman-filtered mean-reverting spread z-score."""

    id          = 'S_partial_cointegration_pairs'
    name        = 'PartialCointegrationPairs'
    description = 'Partial cointegration pairs: Kalman-filter spread decomposition, trade mean-reverting z-score.'
    tier        = 2
    min_lookback = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    TOP_PAIRS  = 20
    ENTRY_Z    = 2.0
    BASE_SIZE  = 0.01  # 1% per leg

    def _kalman_filter_mr(self, spread: np.ndarray, rho: float,
                          sigma_rw: float, sigma_mr: float) -> tuple:
        """Run Kalman filter; return (filtered_mr_array, log_likelihood)."""
        n = len(spread)
        F = np.array([[1.0, 0.0], [0.0, rho]])
        H = np.array([1.0, 1.0])
        Q = np.diag([sigma_rw ** 2, sigma_mr ** 2])
        R = 1e-4
        x = np.zeros(2)
        P = np.eye(2)
        mr = np.zeros(n)
        ll = 0.0
        for t in range(n):
            xp = F @ x
            Pp = F @ P @ F.T + Q
            inn = spread[t] - H @ xp
            S = float(H @ Pp @ H) + R
            K = (Pp @ H) / S
            x = xp + K * inn
            P = (np.eye(2) - np.outer(K, H)) @ Pp
            mr[t] = x[1]
            ll -= 0.5 * (np.log(2 * np.pi * S) + inn ** 2 / S)
        return mr, ll

    def _fit_pair(self, spread: np.ndarray) -> tuple:
        """Fit partial-cointegration state-space model; return (rho, s_rw, s_mr, lr_stat)."""
        def neg_ll_h1(p):
            rho = float(np.tanh(p[0]))
            srw = float(np.exp(p[1]))
            smr = float(np.exp(p[2]))
            _, ll = self._kalman_filter_mr(spread, rho, srw, smr)
            return -ll

        def neg_ll_h0(p):
            rho = float(np.tanh(p[0]))
            smr = float(np.exp(p[1]))
            _, ll = self._kalman_filter_mr(spread, rho, 1e-8, smr)
            return -ll

        try:
            r1 = minimize(neg_ll_h1, [0.0, -2.0, -2.0], method='Nelder-Mead',
                          options={'maxiter': 300, 'xatol': 1e-4, 'fatol': 1e-4})
            ll1 = -r1.fun
            r0 = minimize(neg_ll_h0, [0.0, -2.0], method='Nelder-Mead',
                          options={'maxiter': 200, 'xatol': 1e-4, 'fatol': 1e-4})
            ll0 = -r0.fun
        except Exception:
            return 0.5, 0.01, 0.01, 0.0

        rho    = float(np.tanh(r1.x[0]))
        s_rw   = float(np.exp(r1.x[1]))
        s_mr   = float(np.exp(r1.x[2]))
        lr     = float(max(2.0 * (ll1 - ll0), 0.0))
        return rho, s_rw, s_mr, lr

    def generate_signals(self, prices: pd.DataFrame, regime: dict,
                         universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (universe<10: {len(tickers)})', file=sys.stderr)
            return []
        if len(prices) < self.min_lookback:
            print(f'[debug] signals=0 (history<504: {len(prices)})', file=sys.stderr)
            return []

        log_px = np.log(prices[tickers].ffill().dropna(how='all').tail(self.min_lookback))
        if len(log_px) < 252:
            print(f'[debug] signals=0 (post-dropna<252)', file=sys.stderr)
            return []

        # Pre-screen: top correlated pairs by log-return correlation
        rets = log_px.diff().dropna()
        corr = rets.corr().abs()
        np.fill_diagonal(corr.values, 0)
        upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
        candidates = list(upper.stack().nlargest(50).index)[:50]

        if not candidates:
            print(f'[debug] signals=0 (no candidates)', file=sys.stderr)
            return []

        # Formation on first half, signals on second half
        mid = len(log_px) // 2
        form = log_px.iloc[:mid]
        trade = log_px.iloc[mid:]

        fitted = []
        for (i, j) in candidates[:30]:
            xi_f = form[i].values
            xj_f = form[j].values
            vj = np.var(xj_f)
            if vj < 1e-8:
                continue
            beta = float(np.cov(xi_f, xj_f)[0, 1] / vj)
            spr = xi_f - beta * xj_f
            spr_std = spr.std()
            if spr_std < 1e-8:
                continue
            spr_norm = (spr - spr.mean()) / spr_std
            rho, s_rw, s_mr, lr = self._fit_pair(spr_norm)
            if lr > 0.0 and 0.0 < rho < 1.0:
                fitted.append((i, j, beta, rho, s_rw, s_mr, lr))

        if not fitted:
            print(f'[debug] signals=0 (no pairs passed LR gate)', file=sys.stderr)
            return []

        fitted.sort(key=lambda x: x[6], reverse=True)

        signals: List[Signal] = []
        for (i, j, beta, rho, s_rw, s_mr, _lr) in fitted[:self.TOP_PAIRS]:
            xi_t = trade[i].values
            xj_t = trade[j].values
            spr_t = xi_t - beta * xj_t
            spr_std = spr_t.std()
            if spr_std < 1e-8:
                continue
            spr_norm = (spr_t - spr_t.mean()) / spr_std
            mr_hat, _ = self._kalman_filter_mr(spr_norm, rho, s_rw, s_mr)
            window = mr_hat[-60:] if len(mr_hat) >= 60 else mr_hat
            roll_std = float(np.std(window))
            if roll_std < 1e-8:
                continue
            zscore = float((mr_hat[-1] - np.mean(window)) / roll_std)
            if abs(zscore) < self.ENTRY_Z:
                continue

            dir_i = 'SHORT' if zscore > 0 else 'LONG'
            dir_j = 'LONG'  if zscore > 0 else 'SHORT'
            conf  = 'HIGH' if abs(zscore) > 3.0 else ('MED' if abs(zscore) > 2.5 else 'LOW')
            size  = float(self.BASE_SIZE * scale)
            params = {'pair': f'{i}_{j}', 'zscore': round(zscore, 3), 'beta': round(beta, 4)}

            for ticker, direction, lp in [(i, dir_i, xi_t[-1]), (j, dir_j, xj_t[-1])]:
                price = float(np.exp(lp))
                stops = self.compute_stops_and_targets(
                    prices[ticker].dropna().tail(20), direction, price,
                    regime_state=regime_state)
                signals.append(Signal(
                    ticker=ticker, direction=direction,
                    entry_price=price,
                    stop_loss=float(stops['stop']),
                    target_1=float(stops['t1']),
                    target_2=float(stops['t2']),
                    target_3=float(stops['t3']),
                    position_size_pct=size,
                    confidence=conf,
                    signal_params=params,
                ))

            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
