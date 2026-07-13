"""
Breadth Divergence Timing — SPY timer off internal market breadth.

Hypothesis: index-level price is a cap-weighted illusion; breadth (the % of
the liquid pool above its 50d SMA) reveals the median stock. Two edges:
(1) NEGATIVE DIVERGENCE — SPY up >2% over 20d while breadth < 45% means a
handful of mega-caps mask broad distribution -> SHORT SPY (3-day
confirmation). (2) WASHOUT REBOUND — breadth < 15% is capitulation-grade;
the first 5-day breadth improvement after such a washout marks the turn ->
LONG SPY. 10d cooldown per side, recomputed statelessly from the panel.

Trades SPY ONLY (instrument_class='etp'). Breadth pool via _extra_panels
liquid_pool; close panel only.
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

__all__ = ['BreadthDivergenceTiming']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_breadth_divergence_timing'


class BreadthDivergenceTiming(BaseStrategy):
    """SHORT SPY on breadth-negative divergence; LONG SPY on washout rebound."""

    id                = STRATEGY_ID
    name              = 'Breadth Divergence Timing'
    description       = ('SPY timer: SHORT on 20d SPY strength (+2%) with breadth <45% (3-day '
                         'confirmation); LONG on first 5-day breadth improvement after a <15% '
                         'washout. Breadth = % of liquid pool above 50d SMA. 10d cooldown.')
    tier              = 3
    signal_frequency  = 'daily'
    # Washout rebounds fire precisely in stressed tape — keep all four regimes
    # and let position_scale discount CRISIS sizing.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    min_lookback      = 120
    MAX_SIGNALS       = 1

    SMA_DAYS         = 50
    RET_DAYS         = 20
    BREADTH_DIV      = 0.45
    SPY_RET_MIN      = 0.02
    WASHOUT_THRESH   = 0.15
    WASHOUT_WINDOW   = 15
    IMPROVE_DAYS     = 5
    CONFIRM_DAYS     = 3
    COOLDOWN_DAYS    = 10
    BASE_SIZE_LONG   = 0.70   # single-instrument timer sizing (house 0.5-0.95 band)
    BASE_SIZE_SHORT  = 0.55

    def default_parameters(self) -> dict:
        return {
            'sma_days':       self.SMA_DAYS,
            'breadth_div':    self.BREADTH_DIV,
            'spy_ret_min':    self.SPY_RET_MIN,
            'washout_thresh': self.WASHOUT_THRESH,
            'confirm_days':   self.CONFIRM_DAYS,
            'cooldown_days':  self.COOLDOWN_DAYS,
            'pool_size':      500,
        }

    @staticmethod
    def _equity_rows(prices: pd.DataFrame) -> pd.DataFrame:
        """Drop union-calendar rows where every equity column is NaN."""
        eq_cols = [c for c in prices.columns
                   if not str(c).startswith('^') and '-USD' not in str(c)
                   and '=F' not in str(c) and '=X' not in str(c)]
        if not eq_cols:
            return prices
        return prices.loc[prices[eq_cols].notna().any(axis=1).values]

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

        if 'SPY' not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Bound per-bar work: only ~min_lookback trailing equity rows are used,
        # so pre-slice a raw tail before the full-panel mask/copy. Union
        # calendar adds <= 2 weekend rows per 5 weekdays, so 350 raw rows always
        # contain >= min_lookback equity rows whenever the full panel would.
        if len(prices) > 350:
            prices = prices.iloc[-350:]
        eq = self._equity_rows(prices)
        if len(eq) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        p = self.parameters
        pool = liquid_pool(prices, max_names=int(p.get('pool_size', 500)))
        pool = [t for t in pool if t in eq.columns and t != 'SPY']
        if len(pool) < 50:
            print('[debug] signals=0', file=sys.stderr)
            return []

        sma_days = int(p.get('sma_days', self.SMA_DAYS))
        cooldown = int(p.get('cooldown_days', self.COOLDOWN_DAYS))
        confirm  = int(p.get('confirm_days', self.CONFIRM_DAYS))
        tail     = sma_days + cooldown + self.WASHOUT_WINDOW + self.IMPROVE_DAYS + 10

        c = eq[pool].iloc[-tail:].astype('float64')
        sma = c.rolling(sma_days, min_periods=int(sma_days * 0.8)).mean()
        above = (c > sma) & sma.notna()
        valid = (c.notna() & sma.notna()).sum(axis=1)
        with np.errstate(divide='ignore', invalid='ignore'):
            breadth = above.sum(axis=1) / valid
        breadth = breadth.where(valid >= 50)

        spy = eq['SPY'].iloc[-tail:].astype('float64')
        ret_days = int(p.get('ret_days', self.RET_DAYS)) if 'ret_days' in p else self.RET_DAYS
        spy_ret = spy / spy.shift(ret_days) - 1.0

        # SHORT leg: negative divergence, confirmed for `confirm` consecutive bars.
        div = (spy_ret > float(p.get('spy_ret_min', self.SPY_RET_MIN))) & \
              (breadth < float(p.get('breadth_div', self.BREADTH_DIV)))
        div = div.fillna(False)
        short_fire = div.copy()
        for k in range(1, confirm):
            short_fire = short_fire & div.shift(k).fillna(False)

        # LONG leg: washout then FIRST 5-day breadth improvement.
        washout = (breadth.rolling(self.WASHOUT_WINDOW, min_periods=5).min()
                   < float(p.get('washout_thresh', self.WASHOUT_THRESH)))
        improve = breadth > breadth.shift(self.IMPROVE_DAYS)
        long_fire = (washout & improve & ~improve.shift(1).fillna(False)).fillna(False)

        def _cooled(fire: pd.Series) -> bool:
            """True if the trigger fired today and NOT within the cooldown window."""
            if len(fire) < cooldown + 2 or not bool(fire.iloc[-1]):
                return False
            return not bool(fire.iloc[-(cooldown + 1):-1].any())

        direction = None
        if _cooled(long_fire):
            direction, base = 'LONG', self.BASE_SIZE_LONG
        elif _cooled(short_fire):
            direction, base = 'SHORT', self.BASE_SIZE_SHORT

        if direction is None:
            print('[debug] signals=0', file=sys.stderr)
            return []

        spy_series = eq['SPY'].dropna()
        if len(spy_series) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []
        price = float(spy_series.iloc[-1])
        if not np.isfinite(price) or price <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        b_now = float(breadth.iloc[-1]) if np.isfinite(breadth.iloc[-1]) else None
        r_now = float(spy_ret.iloc[-1]) if np.isfinite(spy_ret.iloc[-1]) else None
        if direction == 'LONG':
            b_min = float(breadth.iloc[-self.WASHOUT_WINDOW:].min())
            conf = 'HIGH' if b_min < 0.10 else 'MED'
            mode = 'washout_rebound'
        else:
            conf = 'HIGH' if (b_now is not None and b_now < 0.40
                              and r_now is not None and r_now > 0.04) else 'MED'
            mode = 'negative_divergence'

        scale = self.position_scale(regime_state)
        stops = self.compute_stops_and_targets(spy_series, direction, price,
                                               regime_state=regime_state)
        signal = Signal(
            ticker='SPY',
            direction=direction,
            entry_price=price,
            stop_loss=stops['stop'],
            target_1=stops['t1'],
            target_2=stops['t2'],
            target_3=stops['t3'],
            position_size_pct=round(base * scale, 6),
            confidence=conf,
            signal_params={
                'mode':        mode,
                'breadth':     round(b_now, 4) if b_now is not None else None,
                'spy_ret_20d': round(r_now, 4) if r_now is not None else None,
                'pool_size':   len(pool),
            },
        )
        print('[debug] signals=1', file=sys.stderr)
        return [signal]
