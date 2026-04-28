from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['LeastSquaresRiskParity']


class LeastSquaresRiskParity(BaseStrategy):
    """Risk parity portfolios equalize each asset's marginal risk contribution via LS alternating linearization."""

    id          = 'S_least_squares_risk_parity'
    name        = 'LeastSquaresRiskParity'
    description = ('Risk parity portfolios equalize each asset marginal risk contribution, '
                   'providing superior diversification; least-squares alternating linearization '
                   'makes this tractable even under general weight bounds.')
    tier        = 2
    min_lookback = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    LOOKBACK   = 252
    MAX_WEIGHT = 0.20
    MIN_WEIGHT = 0.001
    MAX_ITER   = 30
    TOL        = 1e-6

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

        # Filter to available tickers; require complete history
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 5:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        closes = prices[tickers].tail(self.LOOKBACK + 1).dropna(axis=1, how='any')
        tickers = list(closes.columns)
        n = len(tickers)

        # Safety: require at least 3× observations vs assets (avoids underdetermined cov)
        if len(closes) < max(60, 3 * n):
            print(f'[debug] signals=0 (insufficient history: {len(closes)} rows, {n} assets)', file=sys.stderr)
            return []

        returns = closes.pct_change().dropna()
        cov = returns.cov().values  # n×n

        w = self._solve_ls_risk_parity(cov, n)
        if w is None:
            print(f'[debug] signals=0 (solver failed)', file=sys.stderr)
            return []

        current_prices = closes.iloc[-1]
        sorted_idx = np.argsort(w)[::-1]
        n_top = max(1, n)
        conf_thresholds = (n_top // 3, 2 * n_top // 3)

        signals: List[Signal] = []
        for rank, i in enumerate(sorted_idx):
            ticker = tickers[i]
            wt = float(w[i])
            if wt < self.MIN_WEIGHT:
                continue
            price = float(current_prices.iloc[i])
            if price <= 0:
                continue
            stops = self.compute_stops_and_targets(
                closes[ticker], 'LONG', price, regime_state=regime_state
            )
            confidence = 'HIGH' if rank < conf_thresholds[0] else ('MED' if rank < conf_thresholds[1] else 'LOW')
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=round(price, 4),
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=min(wt * scale, self.MAX_WEIGHT),
                confidence=confidence,
                signal_params={'ls_rp_weight': round(wt, 6), 'n_assets': n},
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    def _solve_ls_risk_parity(self, cov: np.ndarray, n: int):
        """Alternating linearization: iteratively solve linearized QP for LS risk parity."""
        try:
            w = np.ones(n) / n
            lb, ub = self.MIN_WEIGHT, self.MAX_WEIGHT

            for _ in range(self.MAX_ITER):
                sigma2 = float(w @ cov @ w)
                if sigma2 <= 0:
                    return None
                sigma_p = sigma2 ** 0.5
                target_rc = sigma_p / n
                mrc = cov @ w   # marginal risk contributions at w_k

                # Linearised LS obj: ||diag(mrc) w - target_rc * 1||^2
                ATA = np.diag(mrc) @ np.diag(mrc) + 1e-8 * np.eye(n)
                ATb = np.diag(mrc) @ np.full(n, target_rc)
                w_new = np.linalg.solve(ATA, ATb)
                w_new = self._project_simplex_box(w_new, lb, ub, n)
                if np.max(np.abs(w_new - w)) < self.TOL:
                    w = w_new
                    break
                w = w_new

            w = np.clip(w, lb, ub)
            s = w.sum()
            if s <= 0:
                return None
            return w / s
        except Exception:
            return None

    def _project_simplex_box(self, v: np.ndarray, lb: float, ub: float, n: int) -> np.ndarray:
        """Project v onto {w: sum=1, lb<=w<=ub} via iterative clipping."""
        v = np.clip(v, lb, ub)
        for _ in range(60):
            s = v.sum()
            if abs(s - 1.0) < 1e-9:
                break
            v = np.clip(v - (s - 1.0) / n, lb, ub)
        return v
