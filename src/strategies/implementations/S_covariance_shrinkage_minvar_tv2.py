"""
Covariance Shrinkage: Why the Sample Matrix Lies at Scale
Paper: Melchor Alaiz, H. (2026) — https://hmaquant.substack.com/p/covariance-shrinkage-why-the-sample

Sample covariance matrices estimated from short windows badly distort
minimum-variance portfolio weights. Shrinking the sample covariance ~40%
toward a simple target matrix recovers most of the true weighting:
    Sigma_shrunk = (1 - delta) * Sigma_sample + delta * Sigma_target,  delta ~= 0.4
    w_minvar     = (Sigma_shrunk^-1 * 1) / (1' * Sigma_shrunk^-1 * 1)

Variant 2 of 2 (vs. tv1's constant-correlation / long-only-clipped / simple-
return sibling, and vs. the analytic-rho Ledoit-Wolf / HRP-blended fleet
members S_schur_damped_minvar_shrinkage and S_nco_schur_bridge_minvar):
the paper never specifies what "simple target matrix" means beyond
"simple" — here it is read as the classic Ledoit-Wolf scaled-identity
target (off-diagonals shrunk to zero, diagonal set to the universe's
average sample variance), log returns are used instead of simple returns
(standard for covariance estimation), and the raw unconstrained
min-variance solve is kept as-is (negative weights become SHORT signals,
gross-exposure normalized) rather than clipped to long-only — the paper's
own "50/50 pair" example says nothing about a long-only constraint.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['CovarianceShrinkageMinVarTV2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_covariance_shrinkage_minvar'
LOOKBACK = 504        # spec's min_lookback_required (~2 trading years) — unambiguous
MAX_ASSETS = 60        # T_eff/N ~= 503/60 ~= 8.4x, well above the 3x safety floor
MIN_UNIVERSE = 50      # spec's minimum_universe_size — unambiguous
DELTA = 0.40           # spec's reported shrinkage intensity, taken literally
MIN_WEIGHT = 5e-4      # ignore dust allocations (by absolute gross weight)


def _identity_shrinkage_target(sigma: np.ndarray) -> np.ndarray:
    """Classic Ledoit-Wolf scaled-identity target: off-diagonals shrunk to
    zero, diagonal set to the universe's average sample variance (so total
    variance mass is preserved but all cross-asset correlation structure
    in the target is removed)."""
    n = sigma.shape[0]
    avg_var = float(np.mean(np.diag(sigma))) if n > 0 else 0.0
    return np.eye(n) * avg_var


def _minvar_weights_unconstrained(sigma: np.ndarray) -> np.ndarray:
    """Analytical global minimum-variance weights, left unconstrained
    (negative/short weights preserved), then gross-normalized so that
    sum(|w|) == 1."""
    n = sigma.shape[0]
    try:
        inv_s = np.linalg.pinv(sigma)
        ones = np.ones(n)
        w = inv_s @ ones
        denom = float(ones @ inv_s @ ones)
        w = w / denom if abs(denom) > 1e-12 else np.ones(n) / n
    except Exception:
        w = np.ones(n) / n
    gross = np.sum(np.abs(w))
    return w / gross if gross > 1e-10 else np.ones(n) / n


class CovarianceShrinkageMinVarTV2(BaseStrategy):
    """Fixed-intensity identity-shrinkage toward an unconstrained (long/short)
    global minimum-variance equity portfolio."""

    id                = STRATEGY_ID
    name              = 'CovarianceShrinkageMinVarTV2'
    description       = ('Shrinks the sample covariance 40% toward a scaled-identity '
                         'target and solves for unconstrained global minimum-variance '
                         'weights, emitting LONG/SHORT by weight sign.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'max_assets': MAX_ASSETS, 'delta': DELTA}

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
        lookback = int(self.parameters.get('lookback', LOOKBACK))
        max_assets = int(self.parameters.get('max_assets', MAX_ASSETS))
        delta = float(self.parameters.get('delta', DELTA))

        cols = [t for t in universe if t in prices.columns]
        if len(cols) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (universe {len(cols)} < min {MIN_UNIVERSE})', file=sys.stderr)
            return []

        sub = prices[cols].dropna(axis=1, thresh=lookback)
        if sub.shape[0] < lookback or sub.shape[1] < MIN_UNIVERSE:
            print(f'[debug] signals=0 (shape {sub.shape} < lookback={lookback})', file=sys.stderr)
            return []
        sub = sub.iloc[-lookback:]

        if sub.shape[1] > max_assets:
            sub = sub[sub.isna().sum().nsmallest(max_assets).index]
        sub = sub.dropna()

        tickers = list(sub.columns)
        N = len(tickers)
        T_eff = sub.shape[0] - 1   # rows after log-differencing
        if T_eff < 3 * N or N < MIN_UNIVERSE:
            print(f'[debug] signals=0 (underdetermined T={T_eff} N={N})', file=sys.stderr)
            return []

        log_prices = np.log(sub.values)
        log_returns = np.diff(log_prices, axis=0)
        sigma_sample = np.cov(log_returns.T)
        sigma_target = _identity_shrinkage_target(sigma_sample)
        sigma_shrunk = (1.0 - delta) * sigma_sample + delta * sigma_target

        w = _minvar_weights_unconstrained(sigma_shrunk)
        if np.sum(np.abs(w)) < 1e-10:
            print('[debug] signals=0 (zero minvar weights)', file=sys.stderr)
            return []

        last = sub.iloc[-1]
        signals: List[Signal] = []
        for i in np.argsort(-np.abs(w)):
            if len(signals) >= self.MAX_SIGNALS:
                break
            wi = float(w[i])
            if abs(wi) < MIN_WEIGHT:
                continue
            ticker = tickers[i]
            price = float(last[ticker])
            if price <= 0:
                continue
            direction = 'LONG' if wi >= 0 else 'SHORT'
            st = self.compute_stops_and_targets(sub[ticker], direction, price, regime_state=regime_state)
            pos = round(abs(wi) * scale, 4)
            conf = 'HIGH' if abs(wi) > 0.05 else ('MED' if abs(wi) > 0.02 else 'LOW')
            signals.append(Signal(
                ticker=ticker, direction=direction,
                entry_price=price,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=pos,
                confidence=conf,
                signal_params={
                    'delta':  round(delta, 4),
                    'weight': round(wi, 6),
                },
            ))

        print(f'[debug] signals={len(signals)} delta={delta:.2f} N={N}', file=sys.stderr)
        return signals
