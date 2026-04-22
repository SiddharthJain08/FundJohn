"""S-HV8: Gamma-Theta Carry  BUY_VOL when ATM gamma carry exceeds theta bleed.

Gate: gt_ratio = gamma_atm / |theta_atm| >= 1.5 AND iv_rank < 55
      (cheap gamma relative to theta cost  long vol has positive carry)
Data: gamma, theta from options_eod (confirmed live). Zero LLM tokens.
Academic: Black-Scholes P&L decomp (1973); Ramkumar (2025) SSRN 5285239.
"""
from __future__ import annotations
from typing import List
import pandas as pd
from src.strategies.base import BaseStrategy, Signal

GT_RATIO_MIN   = 1.5    # gamma / |theta| threshold
IV_RANK_MAX    = 55     # only buy vol when IV not already elevated
MIN_GAMMA      = 1e-5   # sanity floor


class GammaThetaCarry(BaseStrategy):
    id             = 'S_HV8_gamma_theta_carry'
    name           = 'Gamma-Theta Carry'
    version        = '2.0.0'
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING', 'LOW_VOL']

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        opts_map = (aux_data or {}).get('options', {})
        signals: List[Signal] = []

        for ticker in universe:
            opts = opts_map.get(ticker)
            if opts is None:
                continue

            gamma_atm = opts.get('gamma_atm')
            theta_atm = opts.get('theta_atm')
            iv_rank   = opts.get('iv_rank')
            iv30      = opts.get('iv30')

            if gamma_atm is None or theta_atm is None:
                continue
            if iv_rank is None or iv30 is None:
                continue
            if iv_rank >= IV_RANK_MAX:
                continue
            if gamma_atm < MIN_GAMMA:
                continue

            theta_abs = abs(float(theta_atm))
            if theta_abs < 1e-8:
                continue

            gt_ratio = gamma_atm / theta_abs
            if gt_ratio < GT_RATIO_MIN:
                continue

            if ticker not in prices.columns or len(prices[ticker].dropna()) < 2:
                continue
            ts = prices[ticker].dropna()
            current_price = float(ts.iloc[-1])
            if current_price <= 0:
                continue

            stops = self.compute_stops_and_targets(
                ts, 'LONG', current_price, atr_multiplier=2.0
            )

            scale = self.position_scale(regime_state)
            size  = min(0.02 * min(gt_ratio / GT_RATIO_MIN, 2.0) * scale, 0.06)
            confidence = 'HIGH' if gt_ratio >= 2.5 else 'MED'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'BUY_VOL',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(size, 4),
                confidence        = confidence,
                signal_params     = {
                    'gamma_atm': round(gamma_atm, 6),
                    'theta_atm': round(float(theta_atm), 6),
                    'gt_ratio':  round(gt_ratio, 3),
                    'iv_rank':   iv_rank,
                    'iv30':      iv30,
                },
            ))

        return signals
