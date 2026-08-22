"""
Entropic Value-at-Risk Sector ETF Rotation (Choi 2026)
Source: http://arxiv.org/abs/2608.18022v1

Monthly long-only rebalance across the 11 US sector SPDR ETFs, minimizing
portfolio Entropic Value-at-Risk (EVaR). EVaR(w, z) = inf_{t>0} [ (1/t) *
(log E[exp(t * loss)] - log(1-z)) ] — a Chernoff-bound tail-risk measure that
upper-bounds CVaR and better captures fat-tailed co-movements than Gaussian
mean-variance. The paper fits parametric tempered-stable (NTS/CTS) Levy
distributions per asset via MNTS/ICA and evaluates EVaR analytically from the
fitted cumulant-generating function; this implementation estimates the same
EVaR objective directly from the empirical trailing return distribution
(non-parametric MGF estimate), which converges to the same quantity as the
sample size grows and avoids a from-scratch Levy-parameter fitter inside a
lean signal generator. Portfolio weights are optimized on the simplex via a
single SLSQP call (not covariance-based mean-variance) with a 252-obs / 11-
asset ratio (~23x) well above the 3x floor.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from scipy.optimize import minimize, minimize_scalar
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['EvarTemperedStableSectorEtf']

STRATEGY_ID       = 'S_evar_tempered_stable_sector_etf'
INSTRUMENT_CLASS  = 'etp'
BASKET            = ('XLB', 'XLC', 'XLE', 'XLF', 'XLI', 'XLK', 'XLP', 'XLRE', 'XLU', 'XLV', 'XLY')
LOOKBACK          = 252     # 12-month rolling daily log-returns [pseudocode Step 1]
MIN_ASSETS        = 9       # minimum_universe_size from strategy_spec
MIN_WEIGHT        = 0.02    # prune negligible EVaR-optimal allocations
Z_LEVEL           = 0.95    # EVaR confidence level [pseudocode Step 2]


def _evar_of_losses(losses: np.ndarray, z: float = Z_LEVEL) -> float:
    """Brent-bounded EVaR: inf_{t>0} [(1/t)(log M_L(t) - log(1-z))] [pseudocode Step 2]."""
    log_1mz = np.log(1.0 - z)

    def obj(t: float) -> float:
        if t <= 1e-6:
            return 1e6
        m = float(np.mean(np.exp(np.clip(t * losses, -50.0, 50.0))))
        if m <= 0.0 or not np.isfinite(m):
            return 1e6
        return (np.log(m) - log_1mz) / t

    res = minimize_scalar(obj, bounds=(1e-4, 8.0), method='bounded')
    return float(res.fun) if np.isfinite(res.fun) else 1e6


def _min_evar_weights(log_rets: np.ndarray, z: float = Z_LEVEL) -> np.ndarray:
    """Minimum-EVaR long-only simplex weights [pseudocode Step 3, Algorithm 1]."""
    n = log_rets.shape[1]
    w0 = np.full(n, 1.0 / n)

    def obj(w: np.ndarray) -> float:
        port = log_rets @ w
        return _evar_of_losses(-port, z)

    res = minimize(
        obj, w0, method='SLSQP',
        bounds=[(0.0, 1.0)] * n,
        constraints=[{'type': 'eq', 'fun': lambda w: w.sum() - 1.0}],
        options={'maxiter': 80, 'ftol': 1e-6},
    )
    w = res.x if (res.success and np.all(np.isfinite(res.x))) else w0
    w = np.maximum(w, 0.0)
    total = w.sum()
    return w / total if total > 1e-10 else w0


def _is_last_trading_day_of_month(idx: pd.DatetimeIndex, dt: pd.Timestamp) -> bool:
    same_month = idx[(idx.year == dt.year) & (idx.month == dt.month)]
    return len(same_month) > 0 and same_month.max() == dt


class EvarTemperedStableSectorEtf(BaseStrategy):
    """Minimum-EVaR long-only monthly rotation across 11 US sector SPDR ETFs (Choi 2026)."""

    id                 = STRATEGY_ID
    name               = 'EVaR Tempered-Stable Sector ETF Rotation'
    description        = (
        'Monthly minimum-EVaR long-only allocation across 11 US sector SPDRs; '
        'empirical Chernoff-bound EVaR objective proxies the tempered-stable '
        'Levy MGF from Choi 2026.'
    )
    tier               = 2
    signal_frequency   = 'daily'
    min_lookback       = LOOKBACK
    active_in_regimes  = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    instrument_class   = INSTRUMENT_CLASS
    MAX_SIGNALS        = len(BASKET)

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'z': Z_LEVEL, 'pos_size_frac': 0.60}

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 (no price data)', file=sys.stderr)
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gated)', file=sys.stderr)
            return []
        scale = self.position_scale(regime_state)
        lkbk  = int(self.parameters.get('lookback', LOOKBACK))
        z     = float(self.parameters.get('z', Z_LEVEL))

        available = [t for t in BASKET if t in prices.columns]
        if len(available) < MIN_ASSETS:
            print(f'[debug] signals=0 (basket too small: {len(available)})', file=sys.stderr)
            return []

        last_date = prices.index[-1]
        if not (_is_last_trading_day_of_month(prices.index, last_date) or self.cadence_reset(regime)):
            print('[debug] signals=0 (not a monthly rebalance day)', file=sys.stderr)
            return []

        px = prices[available].tail(lkbk + 1).dropna(axis=1, how='any')
        if px.shape[1] < MIN_ASSETS or len(px) < lkbk:
            print(f'[debug] signals=0 (insufficient history: rows={len(px)} cols={px.shape[1]})', file=sys.stderr)
            return []

        log_rets = np.log(px / px.shift(1)).dropna()
        if len(log_rets) < lkbk // 2:
            print('[debug] signals=0 (too few return obs after diff)', file=sys.stderr)
            return []

        tickers = list(px.columns)
        weights = _min_evar_weights(log_rets.values, z)

        active = sorted(
            [(tickers[i], float(weights[i])) for i in range(len(tickers)) if weights[i] >= MIN_WEIGHT],
            key=lambda x: -x[1],
        )[: self.MAX_SIGNALS]
        if not active:
            print('[debug] signals=0 (all EVaR weights below threshold)', file=sys.stderr)
            return []

        pos_frac = float(self.parameters.get('pos_size_frac', 0.60))
        n_active = len(active)
        equal    = 1.0 / n_active

        signals: List[Signal] = []
        for ticker, w in active:
            series = px[ticker].dropna()
            price = float(series.iloc[-1])
            if price <= 0.0 or not np.isfinite(price):
                continue
            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=price, regime_state=regime_state,
            )
            confidence = 'HIGH' if w >= 1.5 * equal else ('MED' if w >= 0.5 * equal else 'LOW')
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(w * pos_frac * scale, 4),
                confidence        = confidence,
                signal_params     = {
                    'evar_weight': round(w, 4),
                    'z':           z,
                    'regime':      regime_state,
                    'scale':       scale,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
