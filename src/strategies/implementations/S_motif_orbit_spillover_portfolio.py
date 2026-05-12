"""
Motif Orbit Spillover Portfolio — quantile-connectedness orbit-diversity ranking.

Shao, Yang, Zhang (2026). "A Motif-Based Framework for Decomposing Risk Spillovers."
arXiv:2604.25406

For each asset, compute the entropy of its quantile-connectivity profile (lower-tail,
median, upper-tail correlations with all other assets). High entropy = diverse structural
role across regimes = net risk transmitter. Long top quintile, short bottom quintile.
Monthly rebalance.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['MotifOrbitSpilloverPortfolio']


class MotifOrbitSpilloverPortfolio(BaseStrategy):
    """Long high orbit-diversity (transmitters), short low-diversity (receivers) monthly."""

    id                = 'S_motif_orbit_spillover_portfolio'
    name              = 'MotifOrbitSpilloverPortfolio'
    description       = (
        'Orbit-position diversity score via quantile connectedness entropy; '
        'long top-quintile risk-transmitters, short bottom-quintile receivers, monthly rebalance.'
    )
    tier              = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    LOOKBACK   = 252
    REBAL_FREQ = 21
    MIN_ASSETS = 10
    BASE_SIZE  = 0.025

    def default_parameters(self) -> dict:
        return {'lookback': self.LOOKBACK, 'rebal_freq': self.REBAL_FREQ}

    @staticmethod
    def _quantile_conn_entropy(col: np.ndarray, others: np.ndarray) -> float:
        """
        Entropy of 3-quantile connectivity profile for one asset.

        col:    (T,) returns for asset i.
        others: (T, N-1) returns for all j != i.

        Returns scalar entropy in [0, log(3)]. Higher = more structurally diverse
        (participates differently in tail-down, middle, and tail-up regimes),
        approximating high orbit-position diversity in the motif framework.
        """
        p20, p80 = np.percentile(col, 20), np.percentile(col, 80)
        masks = [col < p20, (col >= p20) & (col <= p80), col > p80]
        profile = []
        for mask in masks:
            n_obs = int(mask.sum())
            if n_obs < 5 or float(col[mask].std()) < 1e-8:
                profile.append(0.0)
                continue
            sub_i = col[mask] - col[mask].mean()
            sub_j = others[mask] - others[mask].mean(axis=0)
            std_i = float(sub_i.std()) + 1e-10
            std_j = sub_j.std(axis=0) + 1e-10
            cov = sub_i @ sub_j / n_obs                      # (N-1,)
            valid = std_j > 1e-8
            if not valid.any():
                profile.append(0.0)
                continue
            mean_abs_corr = float(np.abs(cov[valid] / (std_i * std_j[valid])).mean())
            profile.append(mean_abs_corr)
        p = np.array(profile, dtype=float)
        s = p.sum()
        if s < 1e-8:
            return 0.0
        p /= s
        p = np.clip(p, 1e-10, 1.0)
        return float(-np.sum(p * np.log(p)))

    def _orbit_diversity(self, ret: pd.DataFrame) -> pd.Series:
        """Compute orbit-diversity entropy for every ticker in ret."""
        arr     = ret.values
        tickers = ret.columns.tolist()
        n       = len(tickers)
        scores  = {}
        for idx, ticker in enumerate(tickers):
            others_idx   = [j for j in range(n) if j != idx]
            scores[ticker] = self._quantile_conn_entropy(arr[:, idx], arr[:, others_idx])
        return pd.Series(scores)

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

        lookback   = int(self.parameters.get('lookback',   self.LOOKBACK))
        rebal_freq = int(self.parameters.get('rebal_freq', self.REBAL_FREQ))

        # Monthly rebalance gate: fire only when row count is a multiple of rebal_freq
        if len(prices) % rebal_freq != 0:
            print(f'[debug] signals=0 (non-rebal day)', file=sys.stderr)
            return []

        available = [t for t in universe if t in prices.columns]
        if len(available) < self.MIN_ASSETS:
            print(f'[debug] signals=0 (universe too small: {len(available)})', file=sys.stderr)
            return []
        if len(prices) < lookback:
            print(f'[debug] signals=0 (history too short: {len(prices)} < {lookback})', file=sys.stderr)
            return []

        prices_sub = prices[available].tail(lookback)
        ret = prices_sub.pct_change().dropna()
        # Drop zero-variance or all-NaN columns
        ret = ret.loc[:, ret.std() > 1e-6].dropna(axis=1, how='any')
        if len(ret.columns) < self.MIN_ASSETS:
            print(f'[debug] signals=0 (too few valid columns: {len(ret.columns)})', file=sys.stderr)
            return []

        diversity = self._orbit_diversity(ret)
        if diversity.std() < 1e-8:
            print(f'[debug] signals=0 (flat diversity scores)', file=sys.stderr)
            return []

        ranked        = diversity.rank(pct=True)
        long_tickers  = ranked[ranked >= 0.80].index.tolist()
        short_tickers = ranked[ranked <= 0.20].index.tolist()

        scale   = self.position_scale(regime_state)
        size_l  = min(self.BASE_SIZE * scale, 0.08 / max(len(long_tickers),  1))
        size_s  = min(self.BASE_SIZE * scale, 0.08 / max(len(short_tickers), 1))
        last_px = prices_sub.iloc[-1]
        q90     = float(diversity.quantile(0.90))
        signals: List[Signal] = []

        for t in long_tickers:
            px = float(last_px.get(t, np.nan))
            if not np.isfinite(px) or px <= 0:
                continue
            st   = self.compute_stops_and_targets(prices_sub[t], 'LONG', px, regime_state=regime_state)
            div  = float(diversity[t])
            conf = 'HIGH' if div >= q90 else 'MED'
            signals.append(Signal(
                ticker=t, direction='LONG', entry_price=round(px, 4),
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round(size_l, 4), confidence=conf,
                signal_params={'orbit_diversity': round(div, 6), 'regime': regime_state},
            ))

        for t in short_tickers:
            px = float(last_px.get(t, np.nan))
            if not np.isfinite(px) or px <= 0:
                continue
            st  = self.compute_stops_and_targets(prices_sub[t], 'SHORT', px, regime_state=regime_state)
            div = float(diversity[t])
            signals.append(Signal(
                ticker=t, direction='SHORT', entry_price=round(px, 4),
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round(size_s, 4), confidence='MED',
                signal_params={'orbit_diversity': round(div, 6), 'regime': regime_state},
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
