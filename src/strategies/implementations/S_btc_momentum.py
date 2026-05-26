"""
BTC Momentum — reference implementation for SP-3.1 crypto rails.

Single-asset 90-day total-return momentum on spot BTC-USD with two drawdown
controls: an absolute-momentum cash gate (flat when 90d return <= 0) and
20%-annualized vol-target sizing. Proof-of-concept that instrument_class='crypto'
flows end-to-end (data -> signal -> sizer -> crypto executor -> resting stop).
NOT a tuned production alpha.

State: candidate at mint; promoted to live only after a backtest clears the
crypto PROMOTION_THRESHOLDS (min_sharpe 0.5 / max_drawdown 0.40).
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['BtcMomentum']

BASKET       = ('BTC-USD',)
LOOKBACK     = 90       # trailing bars for momentum + vol
VOL_TARGET   = 0.20     # 20% annualized vol target
BASE_SIZE    = 0.10     # base gross fraction (pre vol-target, pre regime-scale)
TRADING_DAYS = 365      # crypto bars are 24/7 (calendar-day cadence)


class BtcMomentum(BaseStrategy):
    """90-day momentum on BTC-USD; long when trending up, else cash."""

    id                = 'S_btc_momentum'
    name              = 'BTC Momentum'
    description       = (
        '90-day total-return momentum on spot BTC-USD with an absolute-momentum '
        'cash gate and 20% vol-target sizing; reference crypto strategy.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    instrument_class  = 'crypto'
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'vol_target': VOL_TARGET, 'base_size': BASE_SIZE}

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or 'BTC-USD' not in prices.columns:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        series = prices['BTC-USD'].dropna()
        lookback = int(self.parameters.get('lookback', LOOKBACK))
        if len(series) < lookback:
            return []

        mom = float(series.iloc[-1]) / float(series.iloc[-lookback]) - 1.0
        if mom <= 0:                       # absolute-momentum gate → cash
            print('[BtcMomentum] mom90d<=0 -> cash, signals=0', file=sys.stderr)
            return []

        current_price = float(series.iloc[-1])
        rets = series.pct_change().dropna().iloc[-lookback:]
        ann_vol = float(rets.std() * np.sqrt(TRADING_DAYS)) if len(rets) > 1 else 0.0
        vol_target = float(self.parameters.get('vol_target', VOL_TARGET))
        vol_weight = min(1.0, vol_target / ann_vol) if ann_vol > 0 else 1.0

        scale    = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('base_size', BASE_SIZE)) * vol_weight * scale, 4)
        if pos_size < 0.001:
            print('[BtcMomentum] pos_size<0.001 -> signals=0', file=sys.stderr)
            return []

        st = self.compute_stops_and_targets(
            series, direction='LONG', current_price=current_price, regime_state=regime_state)

        confidence = 'HIGH' if mom > 0.20 else ('MED' if mom > 0.05 else 'LOW')

        sig = Signal(
            ticker            = 'BTC-USD',
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = float(st['stop']),
            target_1          = float(st['t1']),
            target_2          = float(st['t2']),
            target_3          = float(st['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'momentum_90d': round(mom, 4),
                'ann_vol':      round(ann_vol, 4),
                'vol_weight':   round(vol_weight, 4),
                'regime':       regime_state,
            },
        )
        print(f'[BtcMomentum] mom90d={mom:.3f} vol={ann_vol:.3f} size={pos_size} '
              f'regime={regime_state}', file=sys.stderr)
        return [sig]
