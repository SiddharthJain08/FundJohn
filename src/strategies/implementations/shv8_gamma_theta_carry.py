"""S-HV8: Gamma-Theta Carry  BUY_VOL when ATM gamma carry exceeds theta bleed.

Gate: gt_ratio = gamma_atm / |theta_atm| >= 1.5 AND iv_rank < 55
      (cheap gamma relative to theta cost  long vol has positive carry)
Data: gamma, theta from options_eod (confirmed live). Zero LLM tokens.
Academic: Black-Scholes P&L decomp (1973); Ramkumar (2025) SSRN 5285239.
"""
from __future__ import annotations
from typing import Any
from src.strategies.base import BaseStrategy, Signal

GT_RATIO_MIN   = 1.5    # gamma / |theta| threshold
IV_RANK_MAX    = 55     # only buy vol when IV not already elevated
MIN_GAMMA      = 1e-5   # sanity floor


class GammaThetaCarry(BaseStrategy):
    id             = 'S_HV8_gamma_theta_carry'
    name           = 'Gamma-Theta Carry'
    version        = '2.0.0'
    regime_filter  = ['HIGH_VOL', 'NEUTRAL', 'LOW_VOL']

    def generate(self, aux_data: dict[str, Any]) -> list[Signal]:
        prices   = aux_data.get('prices', {})
        opts_map = aux_data.get('options', {})
        regime   = aux_data.get('regime', {})
        regime_state = regime.get('state', 'LOW_VOL')

        if not self.should_run(regime_state):
            return []

        signals: list[Signal] = []

        for ticker, opts in opts_map.items():
            gamma_atm = opts.get('gamma_atm')
            theta_atm = opts.get('theta_atm')
            iv_rank   = opts.get('iv_rank')
            iv30      = opts.get('iv30')

            # Gate: both Greeks must be live
            if gamma_atm is None or theta_atm is None:
                continue
            if iv_rank is None or iv30 is None:
                continue

            # IV rank cap  don't buy already-expensive vol
            if iv_rank >= IV_RANK_MAX:
                continue

            # Gamma must be positive and theta negative (standard sign convention)
            if gamma_atm < MIN_GAMMA:
                continue
            theta_abs = abs(float(theta_atm))
            if theta_abs < 1e-8:
                continue

            gt_ratio = gamma_atm / theta_abs

            if gt_ratio < GT_RATIO_MIN:
                continue

            # Entry price
            ts = prices.get(ticker, [])
            if len(ts) < 2:
                continue
            current_price = float(ts[-1])
            if current_price <= 0:
                continue

            stops = self.compute_stops_and_targets(
                ts, 'LONG', current_price, atr_multiplier=2.0
            )

            scale = self.position_scale(regime_state)
            # Size proportional to gt_ratio advantage (capped)
            size = min(0.02 * min(gt_ratio / GT_RATIO_MIN, 2.0) * scale, 0.06)

            confidence = 'HIGH' if gt_ratio >= 2.5 else 'MED'

            signals.append(Signal(
                ticker          = ticker,
                direction       = 'BUY_VOL',
                entry_price     = current_price,
                stop_loss       = stops['stop'],
                target_1        = stops['t1'],
                target_2        = stops['t2'],
                target_3        = stops['t3'],
                position_size_pct = round(size, 4),
                confidence      = confidence,
                signal_params   = {
                    'gamma_atm':  round(gamma_atm, 6),
                    'theta_atm':  round(float(theta_atm), 6),
                    'gt_ratio':   round(gt_ratio, 3),
                    'iv_rank':    iv_rank,
                    'iv30':       iv30,
                },
            ))

        return signals
