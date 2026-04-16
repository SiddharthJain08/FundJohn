"""S-HV8: Gamma-Theta Carry  STAGING (theta_atm + unusual_flow not yet in aux_data).
Carr & Wu (2009) variance risk premium; theta-gamma carry."""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal


class GammaThetaCarry(BaseStrategy):
    id            = 'S_HV8_gamma_theta_carry'
    name          = 'Gamma-Theta Carry'
    version       = '1.0.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL']

    def default_parameters(self) -> dict:
        return {
            'min_gamma_theta_ratio': 1.5,
            'min_iv_rank':           30.0,
            'max_iv_rank':           70.0,
            'base_size_pct':         0.02,
        }

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        """STAGING: requires theta_atm and unusual_flow (not yet available).
        Full logic implemented  will fire once data is present."""
        if prices is None or prices.empty:
            return []
        regime_state = (regime or {}).get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        options_data = (aux_data or {}).get('options', {})
        scale  = self.position_scale(regime_state)
        p      = self.parameters
        signals = []

        for ticker in universe:
            if ticker not in prices.columns:
                continue
            opts = options_data.get(ticker, {})
            if not opts:
                continue
            gamma_atm    = opts.get('gamma_atm')
            theta_atm    = opts.get('theta_atm')       # NOT YET AVAILABLE
            unusual_flow = opts.get('unusual_flow')    # NOT YET AVAILABLE
            iv_rank      = opts.get('iv_rank', 50.0)

            if gamma_atm is None or theta_atm is None or unusual_flow is None:
                continue  # staging guard  missing data keeps this silent
            if not unusual_flow:
                continue
            if iv_rank < p['min_iv_rank'] or iv_rank > p['max_iv_rank']:
                continue
            theta_abs = abs(float(theta_atm))
            if theta_abs < 1e-6:
                continue
            gt_ratio = float(gamma_atm) / theta_abs
            if gt_ratio < p['min_gamma_theta_ratio']:
                continue

            ts            = prices[ticker].dropna()
            current_price = float(ts.iloc[-1])
            stops         = self.compute_stops_and_targets(ts, 'LONG', current_price, atr_multiplier=2.0)
            conf          = 'HIGH' if gt_ratio >= 2.5 else 'MED'
            size          = min(p['base_size_pct'] * (gt_ratio / 3.0) * scale, 0.04)
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'BUY_VOL',
                entry_price       = current_price,
                stop_loss         = current_price * 0.93,
                target_1          = current_price * 1.05,
                target_2          = current_price * 1.10,
                target_3          = current_price * 1.18,
                position_size_pct = round(size, 4),
                confidence        = conf,
                signal_params     = {
                    'gamma_atm':    round(float(gamma_atm), 6),
                    'theta_atm':    round(float(theta_atm), 6),
                    'gt_ratio':     round(gt_ratio, 4),
                    'iv_rank':      round(iv_rank, 2),
                    'unusual_flow': unusual_flow,
                },
            ))
        signals.sort(key=lambda s: s.signal_params.get('gt_ratio', 0), reverse=True)
        return signals[:4]
