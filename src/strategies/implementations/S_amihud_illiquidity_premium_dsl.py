"""S_amihud_illiquidity_premium_dsl - the expression-DSL twin of
``S_amihud_illiquidity_premium`` (Amihud 2002): LONG the most-illiquid
quintile, SHORT the least-illiquid, inside the liquid pool, monthly.

Original: ``S_amihud_illiquidity_premium.AmihudIlliquidityPremium``. The score
is ``rank_pct(sma(abs(pct_change(close, 1)) / (close * volume), 60))``; the
legs use the original's INCLUSIVE 0.80 / 0.20 cut-offs and per-leg count via a
``select_signals`` override (the base class compares strictly).

Divergences from the original (documented, not bugs):
  - A ticker with no bar on the evaluation row emits nothing (the original
    uses its stale last value).
  - The DSL averages the last 60 VALID observations per ticker; the original
    takes the valid cells among the last 60 rows and needs >= 40 of them.
  - Confidence is a flat 'MED'; leg sizes are SIGNAL_BASE_SIZE, not the
    original's separate long/short bases.
"""
from __future__ import annotations

import pandas as pd

from strategies.base import BaseStrategy

try:
    from strategies.implementations._extra_panels import load_wide, liquid_pool
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import load_wide, liquid_pool

__all__ = ['AmihudIlliquidityPremiumDsl']


class AmihudIlliquidityPremiumDsl(BaseStrategy):
    id                = 'S_amihud_illiquidity_premium_dsl'
    name              = 'Amihud Illiquidity Premium (DSL)'
    description       = ('Amihud ILLIQ 60d mean ranked within the liquid pool as a SIGNAL formula: LONG '
                         'most-illiquid quintile, SHORT least-illiquid; monthly. DSL twin of '
                         'S_amihud_illiquidity_premium.')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 90
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 24

    FORMATION_DAYS = 60
    LEG_COUNT      = 12

    SIGNAL_COLUMNS   = {'close', 'volume'}
    SIGNAL           = "rank_pct(sma(abs(pct_change(close, 1)) / (close * volume), 60))"
    SIGNAL_DIRECTION = 'both'
    SIGNAL_THRESHOLD = (0.20, 0.80)
    SIGNAL_BASE_SIZE = 0.015

    def default_parameters(self) -> dict:
        return {'formation_days': self.FORMATION_DAYS, 'leg_count': self.LEG_COUNT, 'pool_size': 500}

    def _month_boundary(self, prices: pd.DataFrame) -> bool:
        if len(prices) < 2:
            return False
        return pd.Timestamp(prices.index[-1]).month != pd.Timestamp(prices.index[-2]).month

    def dsl_should_fire(self, prices, regime) -> bool:
        return (len(prices) >= self.min_lookback
                and (self._month_boundary(prices) or self.cadence_reset(regime)))

    def signal_panel(self, prices, universe, aux_data):
        from strategies.signal_expr import make_panel
        fdays = int(self.parameters.get('formation_days', self.FORMATION_DAYS))
        pool = liquid_pool(prices, max_names=int(self.parameters.get('pool_size', 500)),
                           lookback=fdays)
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in prices.columns]
        if len(pool) < 30:
            return None
        eq = prices[pool].dropna(how='all')          # equity sessions only
        if len(eq) < fdays + 2:
            return None
        vol = load_wide('volume', pool)
        if vol.empty:
            return None
        v = vol.loc[:prices.index[-1]].reindex(eq.index)
        common = [t for t in pool if t in v.columns]
        if len(common) < 30:
            return None
        eq, v = eq.iloc[-(fdays + 1):], v.iloc[-(fdays + 1):]
        return make_panel({'close': eq[common].astype('float64'),
                           'volume': v[common].astype('float64')})

    def select_signals(self, values: pd.Series) -> list:
        # Inclusive cut-offs and per-leg head(n_leg), exactly as the original.
        if len(values) < 30:
            return []
        n_leg = min(int(self.parameters.get('leg_count', self.LEG_COUNT)),
                    max(1, int(len(values) * 0.20)))
        longs = values[values >= 0.80].sort_values(ascending=False).head(n_leg)
        shorts = values[values <= 0.20].sort_values(ascending=True).head(n_leg)
        picks = [(t, 'LONG', float(v)) for t, v in longs.items()]
        picks += [(t, 'SHORT', 1.0 - float(v)) for t, v in shorts.items()]
        return picks[:self.MAX_SIGNALS]
