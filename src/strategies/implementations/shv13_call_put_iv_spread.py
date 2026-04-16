"""S-HV13: Call-Put IV Spread Signal (Informed Options Flow Detector).

Cremers & Weinbaum (2010) JFQA  deviations from put-call parity predict
50bps/week: iv_spread = mean_call_iv - mean_put_iv (ATM, front-month).
+spread => bullish informed flow => LONG; -spread => bearish => SHORT.
Pre-computed in engine.py aux loader. Zero LLM tokens.
"""
from __future__ import annotations
from typing import Any
from src.strategies.base import BaseStrategy, Signal

SPREAD_MIN_NEUTRAL  = 0.025
SPREAD_MIN_HIGH_VOL = 0.032   # wider threshold in noisy regimes
TOP_N               = 8


class CallPutIVSpread(BaseStrategy):
    id            = 'S_HV13_call_put_iv_spread'
    name          = 'Call-Put IV Spread'
    version       = '2.0.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL']

    def generate_signals(self, prices, regime, universe, aux_data) -> list[Signal]:
        prices       = prices.get('prices', {})
        regime       = prices.get('regime', {})
        regime_state = regime.get('state', 'LOW_VOL')

        if not self.should_run(regime_state):
            return []

        spread_min = SPREAD_MIN_HIGH_VOL if regime_state == 'HIGH_VOL' else SPREAD_MIN_NEUTRAL

        candidates: list[tuple[float, str, str, float, dict]] = []

        for ticker, opts in aux_data.items():
            iv_spread = opts.get('iv_spread')
            iv_rank   = opts.get('iv_rank')
            if iv_spread is None or iv_rank is None:
                continue
            if abs(iv_spread) < spread_min:
                continue
            direction = 'LONG' if iv_spread > 0 else 'SHORT'
            candidates.append((abs(iv_spread), ticker, direction, iv_spread, opts))

        candidates.sort(key=lambda x: x[0], reverse=True)
        candidates = candidates[:TOP_N]

        signals: list[Signal] = []
        for abs_sp, ticker, direction, spread, opts in candidates:
            ts = prices.get(ticker, [])
            if len(ts) < 5:
                continue
            current_price = float(ts[-1])
            if current_price <= 0:
                continue

            stops  = self.compute_stops_and_targets(ts, direction, current_price, atr_multiplier=1.8)
            scale  = self.position_scale(regime_state)
            size   = min(0.015 + 0.01 * (abs_sp / 0.10) * scale, 0.05)
            iv_rank = opts.get('iv_rank', 50)
            confidence = 'HIGH' if abs_sp >= 0.06 and iv_rank > 50 else 'MED'

            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(size, 4),
                confidence        = confidence,
                signal_params     = {
                    'iv_spread': round(spread, 4),
                    'iv_rank':   iv_rank,
                    'iv30':      opts.get('iv30'),
                },
            ))
        return signals
