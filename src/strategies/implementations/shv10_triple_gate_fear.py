"""S-HV10: Triple-Gate Fear  STAGING (unusual_flow not yet in aux_data). Whaley (2000)."""
from __future__ import annotations
from typing import List
from strategies.base import BaseStrategy, Signal


class TripleGateFear(BaseStrategy):
    id            = 'S_HV10_triple_gate_fear'
    name          = 'Triple-Gate Fear'
    version       = '1.0.0'
    active_in_regimes = ['HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            'min_vrp':       0.08,
            'min_pc_ratio':  1.30,
            'min_iv_rank':   60.0,
            'base_size_pct': 0.03,
        }

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        """STAGING: Gate 3 (unusual_flow) always False until data added  no signals fire."""
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
            vrp          = opts.get('vrp')
            pc_ratio     = opts.get('pc_ratio')
            unusual_flow = opts.get('unusual_flow')   # NOT YET AVAILABLE  None
            iv_rank      = opts.get('iv_rank', 50.0)

            gate1 = vrp is not None and vrp > p['min_vrp']
            gate2 = pc_ratio is not None and pc_ratio > p['min_pc_ratio']
            gate3 = unusual_flow is True                    # always False in staging

            if not (gate1 and gate2 and gate3):
                continue
            if iv_rank < p['min_iv_rank']:
                continue

            ts            = prices[ticker].dropna()
            current_price = float(ts.iloc[-1])
            conf          = 'HIGH' if vrp > 0.15 and pc_ratio > 1.6 else 'MED'
            size          = min(p['base_size_pct'] * min(vrp / 0.10, 2.0) * scale, 0.06)
            signals.append(Signal(
                ticker=ticker, direction='SELL',
                entry_price=current_price, stop_loss=current_price * 1.10,
                target_1=current_price*0.90, target_2=current_price*0.80, target_3=current_price*0.72,
                position_size_pct=round(size, 4), confidence=conf,
                signal_params={
                    'vrp': round(vrp,4), 'pc_ratio': round(pc_ratio,4),
                    'unusual_flow': unusual_flow, 'iv_rank': round(iv_rank,2),
                    'gate1': gate1, 'gate2': gate2, 'gate3': gate3,
                },
            ))
        signals.sort(key=lambda s: s.signal_params.get('vrp', 0), reverse=True)
        return signals[:4]
