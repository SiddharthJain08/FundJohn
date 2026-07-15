"""
ETH/BTC Relative Momentum — weekly 2-coin rotation.

Extends S_btc_momentum's single-asset absolute-momentum gate to a two-asset
rotation: each week, hold whichever of BTC-USD / ETH-USD has the higher
trailing 90-day total return, IF that return is positive; otherwise stay
flat (cash). Sizing mirrors S_btc_momentum: 20% annualized vol-target on the
winner's own trailing 90d realized vol, scaled by regime.

Data: the engine's wide close panel. Crypto trades 24/7, so the UNION
calendar (which carries weekend rows) IS the trading calendar for this
strategy — rows are used as-is, no equity-calendar masking, no liquid_pool.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['EthBtcRelativeMomentum']

INSTRUMENT_CLASS = 'crypto'
STRATEGY_ID      = 'S_eth_btc_relative_momentum'

COINS        = ('BTC-USD', 'ETH-USD')
LOOKBACK     = 90       # trailing bars for momentum + vol
VOL_TARGET   = 0.20     # 20% annualized vol target
BASE_SIZE    = 0.10     # base gross fraction (pre vol-target, pre regime-scale)
TRADING_DAYS = 365      # crypto bars are 24/7 (calendar-day cadence)


class EthBtcRelativeMomentum(BaseStrategy):
    """Weekly: LONG the higher-90d-return coin iff its return > 0, else flat."""

    id                = STRATEGY_ID
    name              = 'ETH/BTC Relative Momentum'
    description       = ('Weekly BTC-USD vs ETH-USD rotation: hold whichever coin has the higher '
                         'trailing 90d return iff positive, else flat; 20% vol-target sizing.')
    tier              = 3
    signal_frequency  = 'weekly'
    min_lookback      = LOOKBACK + 1
    instrument_class  = 'crypto'
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'vol_target': VOL_TARGET, 'base_size': BASE_SIZE}

    @staticmethod
    def _week_boundary(index: pd.DatetimeIndex) -> bool:
        """True on the first bar of a new ISO week (weekly cadence gate).

        The crypto panel is calendar-daily, so this fires on Mondays.
        """
        if len(index) < 2:
            return False
        d1 = pd.Timestamp(index[-1]).isocalendar()
        d0 = pd.Timestamp(index[-2]).isocalendar()
        return (d1.year, d1.week) != (d0.year, d0.week)

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
        if any(c not in prices.columns for c in COINS):
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not self._week_boundary(prices.index):
            print('[debug] signals=0', file=sys.stderr)
            return []

        lookback = int(self.parameters.get('lookback', LOOKBACK))

        moms: dict = {}
        series_map: dict = {}
        for coin in COINS:
            s = prices[coin].dropna()
            if len(s) < lookback + 1:
                print('[debug] signals=0', file=sys.stderr)
                return []
            last = float(s.iloc[-1])
            base = float(s.iloc[-(lookback + 1)])
            if not np.isfinite(last) or not np.isfinite(base) or base <= 0 or last <= 0:
                print('[debug] signals=0', file=sys.stderr)
                return []
            moms[coin] = last / base - 1.0
            series_map[coin] = s

        # Winner-take-all rotation with an absolute-momentum cash gate.
        winner = max(COINS, key=lambda c: moms[c])
        mom = moms[winner]
        if mom <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        series = series_map[winner]
        current_price = float(series.iloc[-1])
        rets = series.pct_change().dropna().iloc[-lookback:]
        ann_vol = float(rets.std() * np.sqrt(TRADING_DAYS)) if len(rets) > 1 else 0.0
        vol_target = float(self.parameters.get('vol_target', VOL_TARGET))
        vol_weight = min(1.0, vol_target / ann_vol) if ann_vol > 0 else 1.0

        scale    = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('base_size', BASE_SIZE)) * vol_weight * scale, 4)
        if pos_size < 0.001:
            print('[debug] signals=0', file=sys.stderr)
            return []

        st = self.compute_stops_and_targets(
            series, direction='LONG', current_price=current_price, regime_state=regime_state)

        confidence = 'HIGH' if mom > 0.20 else ('MED' if mom > 0.05 else 'LOW')

        sig = Signal(
            ticker            = winner,
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = float(st['stop']),
            target_1          = float(st['t1']),
            target_2          = float(st['t2']),
            target_3          = float(st['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'momentum_90d':       round(mom, 4),
                'loser_momentum_90d': round(min(moms.values()), 4),
                'ann_vol':            round(ann_vol, 4),
                'vol_weight':         round(vol_weight, 4),
                'regime':             regime_state,
            },
        )
        print('[debug] signals=1', file=sys.stderr)
        return [sig]
