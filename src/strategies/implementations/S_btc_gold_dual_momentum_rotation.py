from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['BtcGoldDualMomentumRotation']


class BtcGoldDualMomentumRotation(BaseStrategy):
    """Dual momentum rotation between IBIT (Bitcoin ETF) and GLD using 4/8/12-week composite signals."""

    id               = 'S_btc_gold_dual_momentum_rotation'
    name             = 'BtcGoldDualMomentumRotation'
    description      = ('Rotate into the stronger store-of-value asset (IBIT or GLD) via composite '
                        '4/8/12-week dual momentum; go flat if both absolute momentum signals are negative.')
    tier             = 2
    signal_frequency = 'weekly'
    min_lookback     = 90  # 12 weeks + buffer
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    LOOKBACKS      = (4, 8, 12)    # weeks
    VOL_TARGET     = 0.20          # 20% annualized vol target
    BASE_SIZE      = 0.80          # max gross allocation
    WEEKS_PER_YEAR = 52.0

    def _composite_sig(self, weekly_series: pd.Series) -> float:
        """Average momentum over 4/8/12-week lookbacks. Returns -inf if insufficient data."""
        rets = []
        for lb in self.LOOKBACKS:
            if len(weekly_series) > lb:
                r = float(weekly_series.iloc[-1] / weekly_series.iloc[-1 - lb] - 1)
                rets.append(r)
        return float(np.mean(rets)) if rets else float('-inf')

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

        has_ibit = 'IBIT' in prices.columns
        has_gld  = 'GLD'  in prices.columns

        if not has_gld:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Resample to weekly Wednesday closes
        cols = [c for c in ['IBIT', 'GLD'] if c in prices.columns]
        weekly = prices[cols].resample('W-WED').last()

        MIN_OBS = max(self.LOOKBACKS) + 2
        if len(weekly) < MIN_OBS:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Compute composite momentum signals
        sig_btc = float('-inf')
        if has_ibit and 'IBIT' in weekly.columns:
            ibit_w = weekly['IBIT'].dropna()
            if len(ibit_w) >= MIN_OBS:
                sig_btc = self._composite_sig(ibit_w)

        sig_gld = float('-inf')
        if 'GLD' in weekly.columns:
            gld_w = weekly['GLD'].dropna()
            if len(gld_w) >= MIN_OBS:
                sig_gld = self._composite_sig(gld_w)

        # Dual momentum allocation rule
        chosen_ticker = None
        chosen_sig    = float('-inf')
        if sig_btc > sig_gld and sig_btc > 0:
            chosen_ticker = 'IBIT'
            chosen_sig    = sig_btc
        elif sig_gld >= sig_btc and sig_gld > 0:
            chosen_ticker = 'GLD'
            chosen_sig    = sig_gld

        if chosen_ticker is None:
            # Both absolute momentum filters fail — go FLAT (cash)
            print('[debug] signals=0', file=sys.stderr)
            return []

        price_series = prices[chosen_ticker].dropna()
        if price_series.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        current_price = float(price_series.iloc[-1])

        # Volatility-targeting weight: min(1, 20% / annualized_vol_12w)
        vol_weight = 1.0
        chosen_weekly = weekly[chosen_ticker].dropna()
        if len(chosen_weekly) >= 13:
            weekly_rets = chosen_weekly.pct_change().dropna().iloc[-13:]
            ann_vol = float(weekly_rets.std() * np.sqrt(self.WEEKS_PER_YEAR))
            if ann_vol > 0:
                vol_weight = min(1.0, self.VOL_TARGET / ann_vol)

        position_size = round(self.BASE_SIZE * vol_weight * scale, 4)
        if position_size < 0.01:
            print('[debug] signals=0', file=sys.stderr)
            return []

        if chosen_sig > 0.10:
            confidence = 'HIGH'
        elif chosen_sig > 0.04:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        st = self.compute_stops_and_targets(
            price_series, 'LONG', current_price, regime_state=regime_state
        )

        signals = [Signal(
            ticker            = chosen_ticker,
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = float(st['stop']),
            target_1          = float(st['t1']),
            target_2          = float(st['t2']),
            target_3          = float(st['t3']),
            position_size_pct = position_size,
            confidence        = confidence,
            signal_params     = {
                'sig_btc':    round(sig_btc, 6) if sig_btc != float('-inf') else None,
                'sig_gld':    round(sig_gld, 6) if sig_gld != float('-inf') else None,
                'vol_weight': round(vol_weight, 4),
                'rebalance':  'weekly',
            },
        )]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
