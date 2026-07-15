"""
Overnight–Intraday Tug of War — cross-sectional return decomposition.

Source: Lou, Polk & Skouras (2019, Journal of Financial Economics),
"A tug of war: Overnight versus intraday expected returns."

Hypothesis: virtually all of momentum's premium accrues OVERNIGHT while
intraday returns revert. Stocks whose recent gains came disproportionately
overnight (open vs prior close) are being accumulated by institutions whose
demand persists; stocks whose gains came intraday reflect transient
day-trader pressure that reverts. LONG the top decile of trailing 60-day
cumulative overnight-minus-intraday return spread, SHORT the bottom decile,
rebalanced monthly.

Data: close panel (engine) + self-loaded OPEN panel from prices.parquet via
_extra_panels (point-in-time sliced, 2021+ coverage window).
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

__all__ = ['OvernightIntradayTugOfWar']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_overnight_intraday_tug_of_war'


class OvernightIntradayTugOfWar(BaseStrategy):
    """LONG high overnight-return share names, SHORT high intraday-share names."""

    id                = STRATEGY_ID
    name              = 'Overnight-Intraday Tug of War'
    description       = ('Cross-sectional overnight-vs-intraday return decomposition (Lou-Polk-Skouras 2019): '
                         'LONG top decile trailing 60d overnight-minus-intraday spread, SHORT bottom decile, monthly.')
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

        if not self._month_boundary(prices):
            print('[debug] signals=0', file=sys.stderr)
            return []

        fdays = int(self.parameters.get('formation_days', self.FORMATION_DAYS))
        pool = liquid_pool(prices, max_names=int(self.parameters.get('pool_size', 500)),
                           lookback=fdays)
        pool = [t for t in pool if t in universe] or pool
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        opens = load_wide('open', pool)
        if opens.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        asof = prices.index[-1]
        opens = opens.loc[:asof]
        closes = prices[[t for t in pool if t in prices.columns]].iloc[-(fdays + 2):]
        opens = opens.reindex(closes.index)

        common = [t for t in closes.columns if t in opens.columns]
        if len(common) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []
        c = closes[common].astype('float64')
        o = opens[common].astype('float64')

        with np.errstate(divide='ignore', invalid='ignore'):
            overnight = np.log(o / c.shift(1))          # close[t-1] -> open[t]
            intraday  = np.log(c / o)                   # open[t] -> close[t]
        spread = overnight - intraday                    # daily tug-of-war spread
        spread = spread.iloc[-fdays:]

        valid = spread.notna().sum() >= self.MIN_VALID_DAYS
        score = spread.sum().where(valid).dropna()
        if len(score) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rank_pct = score.rank(pct=True)
        winners = rank_pct[rank_pct >= 0.90].sort_values(ascending=False).head(self.LEG_COUNT)
        losers  = rank_pct[rank_pct <= 0.10].sort_values(ascending=True).head(self.LEG_COUNT)

        scale = self.position_scale(regime_state)
        current = prices.iloc[-1]

        def _conf(pct: float, long: bool) -> str:
            ext = pct if long else (1.0 - pct)
            if ext >= 0.97:
                return 'HIGH'
            if ext >= 0.93:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for leg, direction, base in ((winners, 'LONG', self.BASE_SIZE_LONG),
                                     (losers, 'SHORT', self.BASE_SIZE_SHORT)):
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
                signals.append(Signal(
                    ticker=ticker,
                    direction=direction,
                    entry_price=price,
                    stop_loss=stops['stop'],
                    target_1=stops['t1'],
                    target_2=stops['t2'],
                    target_3=stops['t3'],
                    position_size_pct=round(base * scale, 6),
                    confidence=_conf(float(rp), direction == 'LONG'),
                    signal_params={
                        'tug_spread_60d': round(float(score[ticker]), 4),
                        'rank_pct':       round(float(rp), 4),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
