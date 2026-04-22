"""S-HV12: VRP Normalization  Carr & Wu (2009); Bollerslev et al. (2009).
VRP z-score mean reversion: sell when fear premium is abnormally high, buy when
complacency pushes VRP negative."""
from __future__ import annotations
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal


class VRPNormalization(BaseStrategy):
    id            = 'S_HV12_vrp_normalization'
    name          = 'VRP Normalization'
    version       = '1.0.0'
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING', 'LOW_VOL']

    def default_parameters(self) -> dict:
        return {
            'zscore_threshold': 1.5,
            'min_vrp_sell':     0.05,
            'max_vrp_buy':      0.00,
            'min_vrp_std':      0.005,
            'min_iv_rank_sell': 50.0,
            'max_iv_rank_buy':  50.0,
            'base_size_pct':    0.02,
        }

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
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
            vrp         = opts.get('vrp')
            vrp_history = opts.get('vrp_history', [])
            iv_rank     = opts.get('iv_rank', 50.0)
            rv_20       = opts.get('rv_20')

            if vrp is None or len(vrp_history) < 5:
                continue

            vrp_mean = float(np.mean(vrp_history))
            vrp_std  = float(np.std(vrp_history))
            if vrp_std < p['min_vrp_std']:
                continue

            vrp_zscore = (vrp - vrp_mean) / vrp_std

            direction = None
            if vrp_zscore > p['zscore_threshold'] and vrp > p['min_vrp_sell'] and iv_rank > p['min_iv_rank_sell']:
                direction = 'SELL_VOL'
            elif vrp_zscore < -p['zscore_threshold'] and vrp < p['max_vrp_buy'] and iv_rank < p['max_iv_rank_buy']:
                direction = 'BUY_VOL'
            if direction is None:
                continue

            ts            = prices[ticker].dropna()
            current_price = float(ts.iloc[-1])
            conf          = 'HIGH' if abs(vrp_zscore) >= 2.5 else 'MED'

            if direction == 'SELL_VOL':
                size = min(p['base_size_pct'] * min(vrp_zscore / 2.0, 2.5) * scale, 0.05)
                sl   = current_price * 1.09
                t1, t2, t3 = current_price*0.92, current_price*0.84, current_price*0.78
            else:
                size = min(p['base_size_pct'] * min(abs(vrp_zscore) / 2.0, 1.5) * scale, 0.03)
                sl   = current_price * 0.93
                t1, t2, t3 = current_price*1.05, current_price*1.10, current_price*1.17

            signals.append(Signal(
                ticker=ticker, direction=direction,
                entry_price=current_price, stop_loss=sl,
                target_1=t1, target_2=t2, target_3=t3,
                position_size_pct=round(max(size, 0.005), 4), confidence=conf,
                signal_params={
                    'vrp': round(vrp,4), 'vrp_zscore': round(vrp_zscore,4),
                    'vrp_mean': round(vrp_mean,4), 'vrp_std': round(vrp_std,4),
                    'iv_rank': round(iv_rank,2), 'rv_20': round(rv_20,4) if rv_20 else None,
                    'signal_type': direction,
                },
            ))
        signals.sort(key=lambda s: abs(s.signal_params.get('vrp_zscore',0)), reverse=True)
        return signals[:5]
