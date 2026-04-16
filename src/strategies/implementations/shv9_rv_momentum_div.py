"""S-HV9: RV Momentum Divergence  Bollerslev, Tauchen & Zhou (2009)."""
from __future__ import annotations
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal


class RVMomentumDivergence(BaseStrategy):
    id            = 'S_HV9_rv_momentum_div'
    name          = 'RV Momentum Divergence'
    version       = '1.0.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL', 'LOW_VOL']

    def default_parameters(self) -> dict:
        return {
            'min_price_mom':  0.02,
            'min_rv_mom_up':  0.10,
            'min_rv_mom_dn': -0.05,
            'min_rv_20':      0.10,
            'base_size_pct':  0.02,
        }

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty or len(prices) < 22:
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
            rv_20        = opts.get('rv_20')
            hv20_history = opts.get('hv20_history', [])
            if rv_20 is None or rv_20 < p['min_rv_20']:
                continue
            if len(hv20_history) < 5:
                continue
            ts = prices[ticker].dropna()
            if len(ts) < 22:
                continue
            current_price = float(ts.iloc[-1])
            price_mom = (float(ts.iloc[-1]) / float(ts.iloc[-6])) - 1.0
            rv_mom    = (hv20_history[-1] / hv20_history[-5]) - 1.0 if hv20_history[-5] > 0 else 0.0

            direction = None
            div_type  = None
            if price_mom > p['min_price_mom'] and rv_mom < p['min_rv_mom_dn']:
                direction = 'BUY'; div_type = 'bullish_divergence'
            elif price_mom < -p['min_price_mom'] and rv_mom > p['min_rv_mom_up']:
                direction = 'SELL'; div_type = 'fear_spike'
            if direction is None:
                continue

            conf = 'HIGH' if abs(price_mom) > 0.04 and abs(rv_mom) > 0.15 else 'MED'
            size = min(max(p['base_size_pct'] * abs(rv_mom) * 2.0, 0.01), 0.04)
            size = round(size * scale, 4)

            if direction == 'BUY':
                sl = current_price * 0.95
                t1, t2, t3 = current_price*1.03, current_price*1.06, current_price*1.10
            else:
                sl = current_price * 1.05
                t1, t2, t3 = current_price*0.97, current_price*0.94, current_price*0.90

            signals.append(Signal(
                ticker=ticker, direction=direction,
                entry_price=current_price, stop_loss=sl,
                target_1=t1, target_2=t2, target_3=t3,
                position_size_pct=size, confidence=conf,
                signal_params={
                    'rv_20': round(rv_20,4), 'rv_mom': round(rv_mom,4),
                    'price_mom': round(price_mom,4), 'divergence_type': div_type,
                },
            ))
        signals.sort(key=lambda s: abs(s.signal_params.get('rv_mom',0)), reverse=True)
        return signals[:5]
