"""S-HV14: OTM Skew Factor (Volatility Smirk Cross-Section).

Xing, Zhang & Zhao (2010) JFQA  smirk = IV(20-delta put) - IV(50-delta call)
predicts 10.9% annual alpha: high skew => informed put buyers => bearish.
SHORT high-skew names in HIGH_VOL regime; avoid or LONG low-skew.
Pre-computed skew_20d in engine.py. Zero LLM tokens.
"""
from __future__ import annotations
from typing import Any
from src.strategies.base import BaseStrategy, Signal

SKEW_SHORT_THRESH  = 0.08   # skew above this => SHORT
SKEW_LONG_THRESH   = 0.02   # skew below this => LONG (complacency)
MIN_IV_RANK        = 40     # only short when vol elevated
TOP_SHORT          = 6
TOP_LONG           = 4


class OTMSkewFactor(BaseStrategy):
    id            = 'S_HV14_otm_skew_factor'
    name          = 'OTM Skew Factor'
    version       = '1.0.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL']

    def generate_signals(self, prices, regime, universe, aux_data) -> list[Signal]:
        prices       = prices.get('prices', {})
        regime       = prices.get('regime', {})
        regime_state = regime.get('state', 'LOW_VOL')

        if not self.should_run(regime_state):
            return []

        shorts: list[tuple[float, str, dict]] = []
        longs:  list[tuple[float, str, dict]] = []

        for ticker, opts in aux_data.items():
            skew_20d = opts.get('skew_20d')
            iv_rank  = opts.get('iv_rank')
            if skew_20d is None or iv_rank is None:
                continue

            if skew_20d >= SKEW_SHORT_THRESH and iv_rank >= MIN_IV_RANK:
                shorts.append((skew_20d, ticker, opts))
            elif skew_20d <= SKEW_LONG_THRESH and iv_rank < 40:
                longs.append((skew_20d, ticker, opts))

        shorts.sort(key=lambda x: x[0], reverse=True)
        longs.sort(key=lambda x: x[0])
        candidates = [(s, t, o, 'SHORT') for s, t, o in shorts[:TOP_SHORT]] + \
                     [(s, t, o, 'LONG')  for s, t, o in longs[:TOP_LONG]]

        signals: list[Signal] = []
        for skew, ticker, opts, direction in candidates:
            ts = prices.get(ticker, [])
            if len(ts) < 5:
                continue
            current_price = float(ts[-1])
            if current_price <= 0:
                continue

            stops = self.compute_stops_and_targets(ts, direction, current_price, atr_multiplier=2.0)
            scale = self.position_scale(regime_state)
            # Size by skew magnitude; 0.08 skew => 2% base
            size  = min(0.015 + 0.005 * (abs(skew) / 0.08) * scale, 0.04)
            iv_rank = opts.get('iv_rank', 50)
            confidence = 'HIGH' if abs(skew) >= 0.12 and iv_rank >= 60 else 'MED'

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
                    'skew_20d': round(skew, 4),
                    'iv_rank':  iv_rank,
                    'vrp':      opts.get('vrp'),
                },
            ))
        return signals
