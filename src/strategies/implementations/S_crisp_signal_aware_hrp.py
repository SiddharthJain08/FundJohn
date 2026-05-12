"""
CRISP Signal-Aware HRP
Paper: "Beyond De Prado and Cotton: Hierarchical and Iterative Methods for
General Mean-Variance Portfolios" — Wuebben (2026)

CRISP: P_γ = (1-γ)·diag(Σ) + γ·Σ, solve P_γ·w = μ, normalize.
α signal μ = 12-1 month cross-sectional momentum.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['CrispSignalAwareHRP']


class CrispSignalAwareHRP(BaseStrategy):
    """CRISP correlation-shrinkage solver with 12-1 month momentum alpha signal."""

    id          = 'S_crisp_signal_aware_hrp'
    name        = 'CrispSignalAwareHRP'
    description = 'CRISP shrinkage (P_γ·w=μ) with 12-1 month momentum alpha, all-regime allocation'
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    min_lookback = 504

    def default_parameters(self) -> dict:
        return {
            'gamma':     0.5,   # correlation shrinkage weight ∈ [0,1]
            'lookback':  252,   # covariance estimation window (days)
            'mom_long':  252,   # momentum lookback (days)
            'mom_skip':   21,   # skip last N days (skip-1-month reversal filter)
            'top_n':      10,   # max signals per direction
            'base_size': 0.02,  # base position size per signal (pre-regime scale)
        }

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

        gamma    = float(self.parameters.get('gamma',     0.5))
        lookback = int(self.parameters.get('lookback',   252))
        mom_long = int(self.parameters.get('mom_long',   252))
        mom_skip = int(self.parameters.get('mom_skip',    21))
        top_n    = int(self.parameters.get('top_n',       10))
        base_sz  = float(self.parameters.get('base_size', 0.02))

        # Intersect universe with available price columns
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 15:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        min_rows = max(lookback, mom_long) + mom_skip + 5
        if len(prices) < min_rows:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Use last min_rows rows; drop tickers with any NaN (stable covariance)
        px_window = prices[tickers].iloc[-min_rows:]
        px_clean  = px_window.dropna(axis=1)
        tickers   = list(px_clean.columns)
        n = len(tickers)
        if n < 15:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Covariance from trailing lookback returns
        ret = px_clean.pct_change().dropna()
        ret_win = ret.iloc[-lookback:]
        if len(ret_win) < lookback // 2:
            print(f'[debug] signals=0', file=sys.stderr)
            return []
        Sigma = ret_win.cov().values  # n×n

        # Alpha signal μ: skip-adjusted momentum
        px_now  = px_clean.iloc[-1].values
        px_skip = px_clean.iloc[-mom_skip].values
        px_base = px_clean.iloc[-mom_long].values
        safe_base = np.where(px_base > 0, px_base, np.nan)
        mu = (px_skip / safe_base) - 1.0
        mu = np.nan_to_num(mu, nan=0.0)

        # CRISP solve: P_γ·w = μ  →  w = P_γ⁻¹·μ
        # Tikhonov regularization guards near-singular P_γ
        P_gamma = (1.0 - gamma) * np.diag(np.diag(Sigma)) + gamma * Sigma
        P_gamma += np.eye(n) * 1e-6

        try:
            w = np.linalg.solve(P_gamma, mu)
        except np.linalg.LinAlgError:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        w_abs_sum = np.sum(np.abs(w))
        if w_abs_sum < 1e-10:
            print(f'[debug] signals=0', file=sys.stderr)
            return []
        w = w / w_abs_sum  # fully-invested normalization

        # Select top_n LONG (highest w) and top_n SHORT (lowest w)
        sorted_idx = np.argsort(w)
        long_idx   = sorted_idx[-top_n:][::-1]
        short_idx  = sorted_idx[:top_n]

        signals: List[Signal] = []
        pos_size = float(min(base_sz * scale, 0.10))

        for idx in long_idx:
            if w[idx] <= 1e-6:
                continue
            ticker  = tickers[idx]
            pseries = prices[ticker].dropna()
            if len(pseries) < 14:
                continue
            cur_price = float(pseries.iloc[-1])
            if cur_price <= 0:
                continue
            st = self.compute_stops_and_targets(
                pseries, 'LONG', cur_price, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker, direction='LONG',
                entry_price=cur_price,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=pos_size,
                confidence='HIGH' if abs(float(w[idx])) >= 0.05 else 'MED',
                signal_params={
                    'gamma': gamma,
                    'weight': round(float(w[idx]), 4),
                    'mu': round(float(mu[idx]), 4),
                },
            ))

        for idx in short_idx:
            if w[idx] >= -1e-6:
                continue
            ticker  = tickers[idx]
            pseries = prices[ticker].dropna()
            if len(pseries) < 14:
                continue
            cur_price = float(pseries.iloc[-1])
            if cur_price <= 0:
                continue
            st = self.compute_stops_and_targets(
                pseries, 'SHORT', cur_price, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker, direction='SHORT',
                entry_price=cur_price,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=pos_size,
                confidence='HIGH' if abs(float(w[idx])) >= 0.05 else 'MED',
                signal_params={
                    'gamma': gamma,
                    'weight': round(float(w[idx]), 4),
                    'mu': round(float(mu[idx]), 4),
                },
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
