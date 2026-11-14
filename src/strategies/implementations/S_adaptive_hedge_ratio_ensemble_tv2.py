"""
Adaptive Hedge Ratio Ensemble (variant 2/2) — tail-beta divergence factor.

Source: Chan & Chu (2026), "Objective Diversification of Dynamic Hedging"
(arXiv:2611.10333). The paper combines 5 objective-specific rolling
hedge-ratio (beta) estimators — minimum-variance, LAD, downside-semivariance,
lower-tail-quantile, and Expected-Shortfall-minimizing — showing each carries
complementary information about true market exposure.

Interpretation (variant 2/2): rather than equal-weighting all 5 estimators
into one consensus beta and trading its LEVEL (the betting-against-beta read
used by variant 1, S_adaptive_hedge_ratio_ensemble), this variant trades the
DIVERGENCE between the paper's tail-conditional objectives (5% ES-minimizing,
20% lower-tail-quantile) and its unconditional objective (minimum-variance).
A stock whose tail-conditional beta sits well ABOVE its unconditional beta
amplifies market stress beyond what its average exposure implies (crash-prone
convexity) and is SHORTed; a stock whose tail-conditional beta sits well
BELOW its unconditional beta is more defensive in stress than its average
exposure implies (crash-resistant) and is LONGed. This is the paper's
"objective diversification carries complementary information" claim used as
a cross-sectional convexity/crash-beta factor rather than as an averaging +
agreement filter — a materially different trade than variant 1's BAB-style
consensus level bet. Rebalanced weekly (vs. variant 1's monthly) and
concentrated in stress-adjacent regimes where tail-beta divergence is most
informative.

Data: close panel + SPY benchmark column only.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import liquid_pool
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import liquid_pool

__all__ = ['AdaptiveHedgeRatioEnsembleTv2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_adaptive_hedge_ratio_ensemble_tv2'


class AdaptiveHedgeRatioEnsembleTv2(BaseStrategy):
    """LONG low tail-vs-unconditional beta divergence / SHORT high divergence (crash-beta factor)."""

    id                = STRATEGY_ID
    name              = 'Adaptive Hedge Ratio Ensemble Tv2'
    description       = ('Tail-beta divergence factor: average of 20%-tail and 5%-ES-tail conditional '
                         'betas minus the unconditional minimum-variance beta vs SPY; LONG the most '
                         'crash-resistant (negative-divergence) names, SHORT the most crash-amplifying '
                         '(positive-divergence) names, weekly rebalance (Chan & Chu 2026, variant 2/2).')
    tier              = 2
    signal_frequency  = 'weekly'
    min_lookback      = 506            # 504d spec window + 2d buffer
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 20

    BETA_WINDOW      = 504
    MIN_OBS_FULL     = 300
    MIN_OBS_TAIL20   = 60
    MIN_OBS_ES05     = 15
    LEG_COUNT        = 10
    BASE_SIZE_LONG   = 0.014
    BASE_SIZE_SHORT  = 0.016
    MARKET_TICKER    = 'SPY'

    def default_parameters(self) -> dict:
        return {'beta_window': self.BETA_WINDOW, 'leg_count': self.LEG_COUNT, 'pool_size': 500}

    def _week_boundary(self, prices: pd.DataFrame) -> bool:
        if len(prices) < 2:
            return False
        w1 = pd.Timestamp(prices.index[-1]).isocalendar()
        w0 = pd.Timestamp(prices.index[-2]).isocalendar()
        return (w1.year, w1.week) != (w0.year, w0.week)

    @staticmethod
    def _masked_beta(r: pd.DataFrame, s: pd.Series, mask: pd.Series, min_obs: int) -> pd.Series:
        """cov(r,s)/var(s) over rows where mask is True, pairwise-valid, vectorized."""
        if r.empty or s.empty or not mask.any():
            return pd.Series(dtype='float64')
        rm = r[mask.values]
        sm = s[mask.values]
        valid = rm.notna()
        n = valid.sum()
        sbc = pd.DataFrame(np.broadcast_to(sm.to_numpy(dtype='float64')[:, None], rm.shape),
                            index=rm.index, columns=rm.columns).where(valid)
        r_mean = rm.mean()
        s_mean = sbc.mean()
        cov = (rm * sbc).mean() - r_mean * s_mean
        var = (sbc * sbc).mean() - s_mean * s_mean
        beta = cov / var.where(var > 1e-10)
        return beta.where(n >= min_obs)

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not (self._week_boundary(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        mkt = self.MARKET_TICKER
        if mkt not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        bwin = int(self.parameters.get('beta_window', self.BETA_WINDOW))
        pool = liquid_pool(prices, max_names=int(self.parameters.get('pool_size', 500)))
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in prices.columns and t != mkt]
        if len(pool) < 100:
            print('[debug] signals=0', file=sys.stderr)
            return []

        eq = prices[pool].dropna(how='all')
        if len(eq) < bwin + 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        c = eq.iloc[-(bwin + 1):].astype('float64')
        spy = prices[mkt].reindex(c.index).astype('float64')
        rets = c.pct_change().iloc[1:]
        sret = spy.pct_change().iloc[1:]
        if sret.notna().sum() < bwin * 0.8:
            print('[debug] signals=0', file=sys.stderr)
            return []

        full_mask = pd.Series(True, index=sret.index)
        q20 = sret.quantile(0.20)
        q05 = sret.quantile(0.05)
        tail20_mask = sret <= q20
        es05_mask = sret <= q05

        b_var    = self._masked_beta(rets, sret, full_mask, self.MIN_OBS_FULL)
        b_tail20 = self._masked_beta(rets, sret, tail20_mask, self.MIN_OBS_TAIL20)
        b_es05   = self._masked_beta(rets, sret, es05_mask, self.MIN_OBS_ES05)

        ensemble = pd.concat({'var': b_var, 'tail20': b_tail20, 'es05': b_es05}, axis=1).dropna(how='any')
        if len(ensemble) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        tail_component = (ensemble['tail20'] + ensemble['es05']) / 2.0
        divergence = tail_component - ensemble['var']

        rank_pct = divergence.rank(pct=True)
        n_leg = min(int(self.parameters.get('leg_count', self.LEG_COUNT)),
                    max(1, int(len(divergence) * 0.20)))
        longs  = rank_pct[rank_pct <= 0.20].sort_values(ascending=True).head(n_leg)
        shorts = rank_pct[rank_pct >= 0.80].sort_values(ascending=False).head(n_leg)

        scale = self.position_scale(regime_state)
        current = c.iloc[-1]

        def _conf(ext: float) -> str:
            if ext >= 0.95:
                return 'HIGH'
            if ext >= 0.88:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for leg, direction, base in ((longs, 'LONG', self.BASE_SIZE_LONG),
                                     (shorts, 'SHORT', self.BASE_SIZE_SHORT)):
            for ticker, rp in leg.items():
                if len(signals) >= self.MAX_SIGNALS:
                    break
                raw = current.get(ticker)
                if raw is None or not np.isfinite(raw) or raw <= 0:
                    continue
                price = float(raw)
                series = prices[ticker].dropna()
                stops = self.compute_stops_and_targets(series, direction, price,
                                                       regime_state=regime_state)
                extremity = 1.0 - float(rp) if direction == 'LONG' else float(rp)
                signals.append(Signal(
                    ticker=ticker,
                    direction=direction,
                    entry_price=price,
                    stop_loss=stops['stop'],
                    target_1=stops['t1'],
                    target_2=stops['t2'],
                    target_3=stops['t3'],
                    position_size_pct=round(base * scale, 6),
                    confidence=_conf(extremity),
                    signal_params={
                        'tail_beta_divergence': round(float(divergence[ticker]), 4),
                        'beta_var':             round(float(ensemble.loc[ticker, 'var']), 4),
                        'beta_tail20':          round(float(ensemble.loc[ticker, 'tail20']), 4),
                        'beta_es05':            round(float(ensemble.loc[ticker, 'es05']), 4),
                        'rank_pct':             round(float(rp), 4),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
