"""
Extending the Oracle Approximating Shrinkage to General Distributions
Paper: Lewis, M.J. (2026) - http://arxiv.org/abs/2610.22144v1

Standard OAS shrinks the sample covariance toward an identity-scaled target
assuming Gaussian returns:
    rho_OAS = ((1-2/p)*tr(S^2) + tr(S)^2) / ((n+1-2/p)*(tr(S^2) - tr(S)^2/p))
Equity returns are heavy-tailed, which biases rho_OAS downward (under-
shrinks). This strategy adds a cumulant-based correction alpha, in [0,1],
that vanishes as returns degenerate toward Gaussianity:
    kappa_basket  = excess_kurtosis(equal-weighted basket LOG return series)
    alpha         = max(kappa_basket, 0) / (max(kappa_basket, 0) + p)
    rho_corrected = clip(rho_OAS + alpha, 0, 1)
    Sigma_shrink  = rho_corrected*(tr(S)/p)*I + (1-rho_corrected)*S
    w             = inv(Sigma_shrink)*1 / (1' * inv(Sigma_shrink) * 1)
LONG the top-decile positive-weight names, SHORT the bottom-decile most
negative-weight names (long-short per the paper's direction_vocab).

Interpretation variant 2 of 2 (abstract-only extraction; the exact
cumulant-correction functional form is unspecified beyond "in [0,1],
vanishes under Gaussianity"). This variant deliberately differs from
sibling variant 1 on every ambiguous choice:
  (a) cumulant proxy: kurtosis of the EQUAL-WEIGHTED BASKET's own log-return
      series (a single joint/portfolio-level statistic) rather than the
      cross-sectional mean of per-asset MARGINAL kurtosis - idiosyncratic
      tail noise cancels in the basket average, so what survives is closer
      to genuine co-movement in the tails (a cheap proxy for the O(p^4)
      joint cumulant tensor the paper actually wants).
  (b) decay mechanism: alpha decays with ASSET COUNT p (kappa/(kappa+p) -
      more names dilute basket-level tail risk) rather than with effective
      SAMPLE SIZE T_eff.
  (c) return convention: LOG returns (log(P_t/P_t-1)), matching the spec's
      explicit separate "log_returns" data requirement, rather than simple
      pct-change returns.
  (d) rebalance cadence: daily rather than weekly - "rebalance periodically"
      is unspecified in the paper; daily is the more literal reading.
  (e) numerical-stability subset: MAX_ASSETS=100 (T_eff/N ~= 7.5x, a wider
      safety margin than variant 1's 6.3x), selected by LOWEST trailing
      realized volatility when available (fewer single-name tail blowups
      contaminating the joint-kurtosis estimate), falling back to data
      completeness when realized_vol aux data is absent.
  (f) signal selection: percentile-based (top/bottom decile of the weight
      distribution, capped) rather than a fixed top-N count per side.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal
from src.strategies.universe_default import r1000 as universe_filter

__all__ = ['OasExtendedCovarianceShrinkageTv2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_oas_extended_covariance_shrinkage'
LOOKBACK = 756          # spec's min_lookback_required (~3 trading years)
MIN_UNIVERSE = 200      # spec's minimum_universe_size
MAX_ASSETS = 100        # T_eff/N ~= 755/100 ~= 7.5x, wider margin than tv1
MIN_WEIGHT = 5e-4       # ignore dust allocations
MAX_SIGNALS_PER_SIDE = 40


def _excess_kurtosis_1d(x: np.ndarray) -> float:
    """Fisher excess kurtosis of a single (de-meaned) return series."""
    c = x - x.mean()
    m2 = float(np.mean(c ** 2))
    m4 = float(np.mean(c ** 4))
    if m2 <= 1e-18:
        return 0.0
    return m4 / (m2 ** 2) - 3.0


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


class OasExtendedCovarianceShrinkageTv2(BaseStrategy):
    """OAS covariance shrinkage corrected by equal-weighted-basket excess
    kurtosis (joint-tail proxy, asset-count decay, log returns); long-short
    global minimum-variance weights."""

    id                = STRATEGY_ID
    name              = 'OasExtendedCovarianceShrinkageTv2'
    description       = ('OAS shrinkage intensity corrected by basket-level excess kurtosis '
                         '(log returns) for heavy-tailed returns; long-short global minvar weights.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    active_in_regimes = ['HIGH_VOL', 'CRISIS', 'TRANSITIONING']

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
            rv_panel = (aux_data or {}).get('realized_vol')
            picked = None
            if isinstance(rv_panel, pd.DataFrame) and not rv_panel.empty:
                common = [t for t in sub.columns if t in rv_panel.columns]
                if len(common) >= max_assets:
                    last_rv = rv_panel[common].iloc[-1].dropna()
                    if len(last_rv) >= max_assets:
                        picked = last_rv.nsmallest(max_assets).index
            if picked is None:
                picked = sub.isna().sum().nsmallest(max_assets).index
            sub = sub[picked]
        sub = sub.dropna()

        tickers = list(sub.columns)
        N = len(tickers)
        log_returns = np.diff(np.log(sub.values), axis=0)
        T_eff = log_returns.shape[0]
        if T_eff < 3 * N or N < 10:
            print(f'[debug] signals=0 (underdetermined T={T_eff} N={N})', file=sys.stderr)
            return []

        sigma_sample = np.cov(log_returns.T)
        p = N

        rho_oas = _oas_rho(sigma_sample, T_eff)
        basket_log_ret = log_returns.mean(axis=1)
        kappa_basket = max(_excess_kurtosis_1d(basket_log_ret), 0.0)
        alpha = kappa_basket / (kappa_basket + p)
        rho_corrected = float(np.clip(rho_oas + alpha, 0.0, 1.0))

        tr_s = float(np.trace(sigma_sample))
        sigma_shrink = (rho_corrected * (tr_s / p)) * np.eye(p) + (1.0 - rho_corrected) * sigma_sample

        w = _minvar_weights(sigma_shrink)
        if np.sum(np.abs(w)) < 1e-10:
            print('[debug] signals=0 (degenerate minvar weights)', file=sys.stderr)
            return []

        last = sub.iloc[-1]
        decile = max(1, min(MAX_SIGNALS_PER_SIDE, N // 10))
        order = np.argsort(w)  # ascending: most-negative first, most-positive last
        long_idx = list(order[::-1][:decile])
        short_idx = list(order[:decile])

        abs_w_sorted = np.sort(np.abs(w))[::-1]
        p95 = abs_w_sorted[max(0, int(0.05 * N) - 1)] if N > 0 else 0.0
        p80 = abs_w_sorted[max(0, int(0.20 * N) - 1)] if N > 0 else 0.0

        signals: List[Signal] = []
        for i in long_idx + short_idx:
            i = int(i)
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
            abs_wi = abs(wi)
            conf = 'HIGH' if abs_wi >= p95 else ('MED' if abs_wi >= p80 else 'LOW')
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
