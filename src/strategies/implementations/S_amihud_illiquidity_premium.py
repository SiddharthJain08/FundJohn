"""
Amihud Illiquidity Premium — harvest the price-impact premium inside the liquid pool.

Source: Amihud (2002, Journal of Financial Markets), "Illiquidity and stock
returns: cross-section and time-series effects."

Hypothesis: expected returns increase in price impact. Amihud illiquidity
ILLIQ = |daily return| / dollar volume measures how much price moves per
traded dollar. Even WITHIN the top-500 liquid pool the spread is wide, and
the relatively illiquid quintile carries a compensation premium over the
mega-liquid quintile. LONG the most-illiquid quintile, SHORT the least-
illiquid, rebalanced monthly. Confining the ranking to the liquid pool keeps
every leg tradable (no microcap positions).

Data: close panel (engine) + self-loaded VOLUME panel from prices.parquet
via _extra_panels (point-in-time sliced, 2021+ coverage window).
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import load_wide, liquid_pool
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import load_wide, liquid_pool

__all__ = ['AmihudIlliquidityPremium']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_amihud_illiquidity_premium'


class AmihudIlliquidityPremium(BaseStrategy):
    """LONG the most-illiquid liquid-pool quintile, SHORT the least-illiquid."""

    id                = STRATEGY_ID
    name              = 'Amihud Illiquidity Premium'
    description       = ('Amihud ILLIQ = |ret|/(close*volume), 60d mean ranked within the liquid-500 pool: '
                         'LONG most-illiquid quintile, SHORT least-illiquid; monthly, <=12/leg.')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 90
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 24

    FORMATION_DAYS  = 60
    MIN_VALID_DAYS  = 40
    LEG_COUNT       = 12
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012

    def default_parameters(self) -> dict:
        return {
            'formation_days': self.FORMATION_DAYS,
            'leg_count':      self.LEG_COUNT,
            'pool_size':      500,
        }

    def _month_boundary(self, prices: pd.DataFrame) -> bool:
        """True on the first trading day of a month (monthly cadence gate)."""
        if len(prices) < 2:
            return False
        d1 = pd.Timestamp(prices.index[-1])
        d0 = pd.Timestamp(prices.index[-2])
        return d1.month != d0.month

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

        if not (self._month_boundary(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        fdays = int(self.parameters.get('formation_days', self.FORMATION_DAYS))
        pool = liquid_pool(prices, max_names=int(self.parameters.get('pool_size', 500)),
                           lookback=fdays)
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in prices.columns]
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Union-calendar safety: equity sessions only.
        eq = prices[pool].dropna(how='all')
        if len(eq) < fdays + 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        vol = load_wide('volume', pool)
        if vol.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        asof = prices.index[-1]

        c = eq.iloc[-(fdays + 1):].astype('float64')
        v = vol.loc[:asof].reindex(c.index)
        common = [t for t in pool if t in v.columns]
        if len(common) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []
        c = c[common]
        v = v[common].astype('float64')

        rets   = c.pct_change().iloc[1:]
        dollar = (c * v).iloc[1:]
        illiq  = rets.abs() / dollar.where(dollar > 0)
        valid  = illiq.notna().sum() >= self.MIN_VALID_DAYS
        score  = illiq.mean().where(valid).dropna()
        score  = score[np.isfinite(score)]
        if len(score) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rank_pct = score.rank(pct=True)
        n_leg = min(int(self.parameters.get('leg_count', self.LEG_COUNT)),
                    max(1, int(len(score) * 0.20)))
        # High ILLIQ = most illiquid = premium harvest -> LONG.
        longs  = rank_pct[rank_pct >= 0.80].sort_values(ascending=False).head(n_leg)
        shorts = rank_pct[rank_pct <= 0.20].sort_values(ascending=True).head(n_leg)

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
                extremity = float(rp) if direction == 'LONG' else 1.0 - float(rp)
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
                        'amihud_60d_x1e9': round(float(score[ticker]) * 1e9, 5),
                        'rank_pct':        round(float(rp), 4),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
