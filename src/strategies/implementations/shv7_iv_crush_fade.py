"""S-HV7: IV Crush Fade  post-peak IV mean reversion (Stein 1989, Bollen & Whaley 2004)."""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal


class IVCrushFade(BaseStrategy):
    id            = 'S_HV7_iv_crush_fade'
    name          = 'IV Crush Fade'
    version       = '1.0.0'
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING']

    def default_parameters(self) -> dict:
        return {
            'iv_rank_threshold': 75.0,
            'min_iv30':          0.25,
            'max_iv30':          1.20,
            'base_size_pct':     0.02,
        }

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = (regime or {}).get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        options_data = (aux_data or {}).get('options', {})
        if not options_data:
            return []
        scale  = self.position_scale(regime_state)
        p      = self.parameters
        signals = []

        for ticker in universe:
            if ticker not in prices.columns:
                continue
            opts = options_data.get(ticker, {})
            if not opts:
                continue
            iv_rank         = opts.get('iv_rank')
            iv30            = opts.get('iv30')
            iv_rank_history = opts.get('iv_rank_history', [])
            if iv_rank is None or iv30 is None:
                continue
            if len(iv_rank_history) < 3:
                continue
            if iv_rank <= p['iv_rank_threshold']:
                continue
            if iv30 < p['min_iv30'] or iv30 > p['max_iv30']:
                continue
            # Confirm declining trend: peak has passed
            h = iv_rank_history[-3:]
            if not (h[0] > h[1] > h[2]):
                continue
            ts            = prices[ticker].dropna()
            current_price = float(ts.iloc[-1])
            stops         = self.compute_stops_and_targets(ts, 'SHORT', current_price, atr_multiplier=2.0)
            stop_loss     = current_price * 1.08
            conf          = 'HIGH' if iv_rank >= 85 else 'MED'
            size          = min(p['base_size_pct'] * (iv_rank / 100.0) * scale, 0.05)
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SELL_VOL',
                entry_price       = current_price,
                stop_loss         = stop_loss,
                target_1          = current_price * 0.85,
                target_2          = current_price * 0.70,
                target_3          = current_price * 0.55,
                position_size_pct = round(size, 4),
                confidence        = conf,
                signal_params     = {
                    'iv_rank':         round(iv_rank, 2),
                    'iv30':            round(iv30, 4),
                    'iv_rank_history': [round(x, 2) for x in h],
                    'volume':          opts.get('volume', 0),
                },
            ))
        signals.sort(key=lambda s: s.signal_params.get('iv_rank', 0), reverse=True)
        return signals[:5]
