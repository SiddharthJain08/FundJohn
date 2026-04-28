from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['PairsTradingJumpDiffusionIntraday']


class PairsTradingJumpDiffusionIntraday(BaseStrategy):
    """OU+jump-diffusion pairs: rank by mean-reversion speed kappa, trade z-score entry/exit thresholds."""

    id          = 'S_pairs_trading_jump_diffusion_intraday'
    name        = 'PairsTradingJumpDiffusionIntraday'
    description = 'OU+jump-diffusion pairs: rank by kappa, trade individualized z-score entry/exit thresholds'
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    LOOKBACK  = 252
    TOP_K     = 5
    ENTRY_Z   = 1.5
    BASE_SIZE = 0.04   # fraction per leg

    def _fit_ou(self, spread: np.ndarray):
        """Fit AR(1) to spread; return (kappa_annual, mu_eq, sigma_eq) or None."""
        y, x = spread[1:], spread[:-1]
        X = np.column_stack([x, np.ones(len(x))])
        coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        b, a = float(coeffs[0]), float(coeffs[1])
        if not (0.0 < b < 1.0):
            return None
        kappa = -np.log(b) * 252.0
        mu_eq = a / (1.0 - b)
        resid = y - (a + b * x)
        sigma_eq = float(np.std(resid)) / max(np.sqrt(1.0 - b ** 2), 1e-8)
        return float(kappa), float(mu_eq), sigma_eq

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        available = [t for t in universe if t in prices.columns]
        if len(available) < 4 or len(prices) < self.LOOKBACK:
            print(f'[debug] signals=0 (n={len(available)}, rows={len(prices)})', file=sys.stderr)
            return []

        log_px = np.log(prices[available].ffill().dropna(how='all'))
        if len(log_px) < self.LOOKBACK:
            print(f'[debug] signals=0 (log dropna reduced rows below lookback)', file=sys.stderr)
            return []

        recent = log_px.iloc[-self.LOOKBACK:].values
        cur_log = log_px.iloc[-1].values
        cur_px  = prices[available].iloc[-1]
        tickers = list(log_px.columns)
        n = len(tickers)

        pair_scores = []
        for i in range(n):
            for j in range(i + 1, n):
                si, sj = recent[:, i], recent[:, j]
                if np.isnan(si).any() or np.isnan(sj).any():
                    continue
                X = np.column_stack([sj, np.ones(len(sj))])
                coeffs, _, _, _ = np.linalg.lstsq(X, si, rcond=None)
                beta, alpha = float(coeffs[0]), float(coeffs[1])
                spread = si - beta * sj - alpha
                result = self._fit_ou(spread)
                if result is None:
                    continue
                kappa, mu_eq, sigma_eq = result
                if sigma_eq < 1e-8 or kappa < 5.0:
                    continue
                cur_spread = float(cur_log[i] - beta * cur_log[j] - alpha)
                zscore = (cur_spread - mu_eq) / sigma_eq
                pair_scores.append((kappa, tickers[i], tickers[j], beta, alpha, mu_eq, sigma_eq, zscore))

        pair_scores.sort(key=lambda x: x[0], reverse=True)
        signals: List[Signal] = []

        for kappa, ti, tj, beta, alpha, mu_eq, sigma_eq, zscore in pair_scores[:self.TOP_K]:
            # Individualized threshold — higher kappa → tighter entry
            entry_z = max(1.0, self.ENTRY_Z - kappa / 500.0)
            if abs(zscore) < entry_z:
                continue
            pi = float(cur_px[ti])
            pj = float(cur_px[tj])
            if pi <= 0.0 or pj <= 0.0:
                continue
            dir_i = 'LONG' if zscore < -entry_z else 'SHORT'
            dir_j  = 'SHORT' if dir_i == 'LONG' else 'LONG'
            conf   = 'HIGH' if abs(zscore) > 2.0 else 'MED'
            size   = round(float(self.BASE_SIZE * scale), 4)
            params = {
                'pair':   f'{ti}/{tj}',
                'zscore': round(float(zscore), 4),
                'kappa':  round(float(kappa), 4),
                'beta':   round(float(beta), 4),
                'entry_z': round(float(entry_z), 4),
            }
            st_i = self.compute_stops_and_targets(
                prices[ti].dropna(), dir_i, pi, regime_state=regime_state
            )
            st_j = self.compute_stops_and_targets(
                prices[tj].dropna(), dir_j, pj, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ti, direction=dir_i, entry_price=pi,
                stop_loss=st_i['stop'], target_1=st_i['t1'], target_2=st_i['t2'], target_3=st_i['t3'],
                position_size_pct=size, confidence=conf, signal_params=params,
            ))
            signals.append(Signal(
                ticker=tj, direction=dir_j, entry_price=pj,
                stop_loss=st_j['stop'], target_1=st_j['t1'], target_2=st_j['t2'], target_3=st_j['t3'],
                position_size_pct=size, confidence=conf, signal_params={**params, 'zscore': round(-float(zscore), 4)},
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
