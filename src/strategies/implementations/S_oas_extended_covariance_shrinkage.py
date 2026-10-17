"""
Extending the Oracle Approximating Shrinkage to General Distributions
Paper: Lewis, M.J. (2026) — http://arxiv.org/abs/2610.22144v1

Standard OAS shrinks the sample covariance toward an identity-scaled target
assuming Gaussian returns:
    rho_OAS = ((1-2/p)*tr(S^2) + tr(S)^2) / ((n+1-2/p)*(tr(S^2) - tr(S)^2/p))
Equity returns are heavy-tailed, which biases rho_OAS downward (under-
shrinks). This strategy adds a cumulant-based correction alpha, estimated
from the cross-sectional average EXCESS KURTOSIS of the trailing-window
returns, that vanishes as the window degenerates toward Gaussian (kappa->0)
and decays as the effective sample size grows (more data -> less correction
needed):
    kappa_bar     = mean_i max(0, excess_kurtosis(returns_i))
    alpha         = kappa_bar / (kappa_bar + T_eff)          # in [0, 1)
    rho_corrected = clip(rho_OAS + alpha, 0, 1)
    Sigma_shrink  = rho_corrected*(tr(S)/p)*I + (1-rho_corrected)*S
    w             = inv(Sigma_shrink)*1 / (1' * inv(Sigma_shrink) * 1)
LONG the top positive-weight names, SHORT the most negative-weight names
(long-short, unlike this fleet's long-only min-variance siblings
S_covariance_shrinkage_minvar / S_schur_damped_minvar_shrinkage /
S_nco_schur_bridge_minvar — the paper's direction_vocab is LONG/SHORT).

Interpretation variant 1 of 2 (abstract-only extraction — the exact
cumulant-correction functional form is not given, only that it is in [0,1]
and vanishes under Gaussianity): this variant (a) uses the cross-sectional
MEAN of per-asset marginal excess kurtosis as a tractable proxy for the true
4th-order JOINT cumulant tensor (which is O(p^4) and intractable at fleet
scale), floored at zero so platykurtic windows never pull shrinkage below
rho_OAS; (b) a sample-size-decaying bound kappa/(kappa+T_eff) rather than a
fixed scale constant; (c) a weekly (Monday) rebalance cadence rather than
daily; (d) a tighter MAX_ASSETS=120 numerical-stability subset (T_eff/N ~=
6.3x, vs. the long-only sibling's 60/8.4x) selected by data completeness
from a >=200-name pre-filtered universe, per the paper's minimum_universe_size.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal
from src.strategies.universe_default import r1000 as universe_filter

__all__ = ['OasExtendedCovarianceShrinkage']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_oas_extended_covariance_shrinkage'
LOOKBACK = 756          # spec's min_lookback_required (~3 trading years)
MIN_UNIVERSE = 200      # spec's minimum_universe_size
MAX_ASSETS = 120        # T_eff/N ~= 755/120 ~= 6.3x, well above the 3x safety floor
MIN_WEIGHT = 5e-4       # ignore dust allocations


def _excess_kurtosis(returns: np.ndarray) -> np.ndarray:
    """Per-column (per-asset) Fisher excess kurtosis of de-meaned returns."""
    centered = returns - returns.mean(axis=0, keepdims=True)
    m2 = np.mean(centered ** 2, axis=0)
    m4 = np.mean(centered ** 4, axis=0)
    m2_safe = np.where(m2 > 1e-18, m2, 1e-18)
    return m4 / (m2_safe ** 2) - 3.0


def _oas_rho(sigma: np.ndarray, n: int) -> float:
    """Chen/Wiesel/Eldar/Hero (2010) analytic OAS shrinkage intensity toward
    an identity-scaled-by-average-variance target."""
    p = sigma.shape[0]
    if p < 3 or n < 1:
        return 1.0
    tr_s = float(np.trace(sigma))
    tr_s2 = float(np.trace(sigma @ sigma))
    num = (1.0 - 2.0 / p) * tr_s2 + tr_s * tr_s
    den = (n + 1.0 - 2.0 / p) * (tr_s2 - (tr_s * tr_s) / p)
    if den <= 1e-18:
        return 1.0
    return float(np.clip(num / den, 0.0, 1.0))


def _minvar_weights(sigma: np.ndarray) -> np.ndarray:
    """Analytical global minimum-variance weights (long-short, sum(w)=1)."""
    n = sigma.shape[0]
    try:
        inv_s = np.linalg.pinv(sigma)
        ones = np.ones(n)
        w = inv_s @ ones
        denom = float(ones @ inv_s @ ones)
        w = w / denom if abs(denom) > 1e-12 else np.ones(n) / n
    except Exception:
        w = np.ones(n) / n
    return w


class OasExtendedCovarianceShrinkage(BaseStrategy):
    """OAS covariance shrinkage with a cumulant (excess-kurtosis) correction
    for heavy-tailed returns; long-short global minimum-variance weights."""

    id                = STRATEGY_ID
    name              = 'OasExtendedCovarianceShrinkage'
    description       = ('OAS shrinkage intensity corrected by cross-sectional excess kurtosis '
                         'for heavy-tailed returns; long-short global minimum-variance weights.')
    tier              = 2
    signal_frequency  = 'weekly'
    min_lookback      = LOOKBACK
    active_in_regimes = ['HIGH_VOL', 'CRISIS', 'TRANSITIONING']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'max_assets': MAX_ASSETS}

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

        # Weekly rebalance: only act on the first trading day of the week.
        last_date = prices.index[-1]
        try:
            is_monday = pd.Timestamp(last_date).weekday() == 0
        except Exception:
            is_monday = True
        if not is_monday:
            print('[debug] signals=0 (not a weekly rebalance day)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        lookback = int(self.parameters.get('lookback', LOOKBACK))
        max_assets = int(self.parameters.get('max_assets', MAX_ASSETS))

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
        returns = sub.pct_change().dropna().values
        T_eff = returns.shape[0]
        if T_eff < 3 * N or N < 10:
            print(f'[debug] signals=0 (underdetermined T={T_eff} N={N})', file=sys.stderr)
            return []

        sigma_sample = np.cov(returns.T)
        p = N

        rho_oas = _oas_rho(sigma_sample, T_eff)
        kappa = _excess_kurtosis(returns)
        kappa_bar = float(np.mean(np.maximum(kappa, 0.0)))
        alpha = kappa_bar / (kappa_bar + T_eff)
        rho_corrected = float(np.clip(rho_oas + alpha, 0.0, 1.0))

        tr_s = float(np.trace(sigma_sample))
        sigma_shrink = (rho_corrected * (tr_s / p)) * np.eye(p) + (1.0 - rho_corrected) * sigma_sample

        w = _minvar_weights(sigma_shrink)
        if np.sum(np.abs(w)) < 1e-10:
            print('[debug] signals=0 (degenerate minvar weights)', file=sys.stderr)
            return []

        last = sub.iloc[-1]
        order = np.argsort(w)  # ascending: most-negative first, most-positive last
        candidates = list(order[::-1][: self.MAX_SIGNALS]) + list(order[: self.MAX_SIGNALS])
        seen = set()
        signals: List[Signal] = []
        for i in candidates:
            i = int(i)
            if i in seen or len(signals) >= self.MAX_SIGNALS:
                continue
            seen.add(i)
            wi = float(w[i])
            if abs(wi) < MIN_WEIGHT:
                continue
            ticker = tickers[i]
            price = float(last[ticker])
            if price <= 0:
                continue
            direction = 'LONG' if wi > 0 else 'SHORT'
            st = self.compute_stops_and_targets(sub[ticker], direction, price, regime_state=regime_state)
            pos = round(min(abs(wi), 1.0) * scale, 4)
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
                    'rho_oas':       round(rho_oas, 6),
                    'alpha':         round(alpha, 6),
                    'rho_corrected': round(rho_corrected, 6),
                    'weight':        round(wi, 6),
                },
            ))

        print(f'[debug] signals={len(signals)} rho_corrected={rho_corrected:.3f} N={N}', file=sys.stderr)
        return signals
