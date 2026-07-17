"""Dual-momentum rate-of-change (oxfordstrat.com/trading-strategies/dual-momentum-rate-of-change/).
Faithful daily-bar rule: long when ROC(n1) > 0 AND ROC(n2) > 0 where n2 = 0.5*n1;
short when both < 0. ROC = 100*(close - close[-n-1])/close[-n-1]. Condition at
close[t]; engine fills at close[t+1]. House brackets. ETF-basket cross-section.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, roc

__all__ = ['OxfDualMomentumRoc']


class OxfDualMomentumRoc(OxfordBaseStrategy):
    id                = 'oxf_dual_momentum_roc'
    name              = 'Oxford Dual-Momentum ROC'
    description       = 'Dual rate-of-change momentum filter (fast = half slow) on liquid ETFs (oxfordstrat dual-momentum-rate-of-change). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 110
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'n1': 100}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        n1 = int(p['n1'])
        n2 = max(int(round(n1 * 0.5)), 1)
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            cseries = bars['close']
            if len(cseries) < n1 + 1:
                continue
            close = float(cseries.iloc[-1])
            r1 = roc(cseries, n1)
            r2 = roc(cseries, n2)
            if r1 != r1 or r2 != r2:
                continue
            if r1 > 0 and r2 > 0:
                direction, edge = 'LONG', r1
            elif r1 < 0 and r2 < 0:
                direction, edge = 'SHORT', -r1
            else:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if edge >= 15.0 else 'MED' if edge >= 5.0 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'n1': n1, 'n2': n2, 'roc_pct': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:dual-momentum-rate-of-change'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
