from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['ConstrainedGMVVCVDynamics']


class ConstrainedGMVVCVDynamics(BaseStrategy):
    """Constrained long-only GMV with EWMA vol dynamics: caps + TC penalty substitute VCV shrinkage."""

    id          = 'S_constrained_gmv_vcv_dynamics'
    name        = 'ConstrainedGMVVCVDynamics'
    description = ('Constrained long-only GMV with EWMA volatility dynamics: asset-level position '
                   'caps substitute VCV shrinkage while EWMA time-series vol is retained on diagonal.')
    tier        = 2
    min_lookback = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    EWMA_SPAN     = 60    # ~3mo window for EWMA time-series vol (paper §3)
    CORR_LOOKBACK = 252   # 1yr window for sample correlation
    MAX_ASSETS    = 30    # 504/30 = 16.8x obs/asset — satisfies 3x guarantee
    POS_CAP       = 0.10  # 10% per-asset cap (practical constraint in paper)
    MIN_WEIGHT    = 0.005 # drop negligible weights
    MAX_ITER      = 50    # simplex projection iterations

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

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 5:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        # Require complete rows over lookback; trim to CORR_LOOKBACK after dropping NaN columns
        closes = prices[tickers].tail(self.CORR_LOOKBACK + 20).dropna(axis=1, how='any').tail(self.CORR_LOOKBACK)
        tickers = list(closes.columns)

        # Dynamic cap: min(available, obs/3, MAX_ASSETS) preserves 3x obs/asset guarantee
        n_obs = len(closes)
        n = min(len(tickers), n_obs // 3, self.MAX_ASSETS)
        if n < 5:
            print(f'[debug] signals=0 (n={n}, obs={n_obs})', file=sys.stderr)
            return []

        returns = closes.pct_change().dropna()

        # EWMA volatility — time-series vol dynamics retained on the covariance diagonal (paper key finding)
        ewma_vol = returns.ewm(span=self.EWMA_SPAN, min_periods=self.EWMA_SPAN // 2).std().iloc[-1]
        if ewma_vol.isna().all() or (ewma_vol <= 0).all():
            print(f'[debug] signals=0 (ewma_vol degenerate)', file=sys.stderr)
            return []

        # Select n lowest-vol assets to seed GMV (min-variance universe selection)
        selected = ewma_vol.dropna().nsmallest(n).index.tolist()
        if len(selected) < 5:
            print(f'[debug] signals=0 (selected<5)', file=sys.stderr)
            return []

        ret_sub = returns[selected]
        ev = ewma_vol[selected].values.astype(float)

        # Build Sigma = diag(ewma_vol) @ sample_corr @ diag(ewma_vol)  [§3-4, EWMA estimator]
        corr = ret_sub.corr().values
        np.fill_diagonal(corr, 1.0)
        Sigma = np.outer(ev, ev) * corr + np.eye(len(selected)) * 1e-8

        # Analytical GMV via linear solve (w* ∝ Sigma^{-1} 1), then project to [0, cap, sum=1]
        n_sel = len(selected)
        try:
            w_raw = np.linalg.solve(Sigma, np.ones(n_sel))
            w_raw = np.maximum(w_raw, 0.0)
        except np.linalg.LinAlgError:
            w_raw = np.ones(n_sel) / n_sel

        w = self._project_box_simplex(w_raw, self.POS_CAP, n_sel)
        current_prices = closes[selected].iloc[-1]
        sorted_idx = np.argsort(w)[::-1]
        n_top = len(sorted_idx)

        signals: List[Signal] = []
        for rank, i in enumerate(sorted_idx):
            ticker = selected[i]
            wt = float(w[i])
            if wt < self.MIN_WEIGHT:
                continue
            price = float(current_prices.iloc[i])
            if price <= 0:
                continue
            stops = self.compute_stops_and_targets(
                closes[ticker], 'LONG', price, regime_state=regime_state
            )
            conf = 'HIGH' if rank < n_top // 3 else ('MED' if rank < 2 * n_top // 3 else 'LOW')
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=round(price, 4),
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=min(wt * scale, self.POS_CAP),
                confidence=conf,
                signal_params={
                    'gmv_weight': round(wt, 6),
                    'ewma_vol':   round(float(ev[i]), 6),
                    'n_assets':   n_sel,
                },
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    def _project_box_simplex(self, v: np.ndarray, cap: float, n: int) -> np.ndarray:
        """Project v onto {w >= 0, sum=1, w_i <= cap} via iterative clip-and-renormalize."""
        w = np.maximum(v, 0.0)
        s = w.sum()
        w = w / s if s > 0 else np.ones(n) / n
        for _ in range(self.MAX_ITER):
            w = np.clip(w, 0.0, cap)
            s = w.sum()
            if s <= 0:
                return np.full(n, 1.0 / n)
            w = w / s
            if np.max(w) <= cap + 1e-9:
                break
        return w
