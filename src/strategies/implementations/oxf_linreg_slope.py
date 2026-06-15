"""Linear-regression slope trend (oxfordstrat.com/trading-strategies/linear-regression/).
Faithful daily-bar rule: long when LRS(n) > 0 AND LRS(n2) > 0; short when both < 0.
LRS = least-squares slope of the last k closes vs. a 0..k-1 time index (raw, not
normalized — per Oxford). Condition at close[t]; engine fills at close[t+1]. House brackets.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, linreg_slope

__all__ = ['OxfLinregSlope']


class OxfLinregSlope(OxfordBaseStrategy):
    id                = 'oxf_linreg_slope'
    name              = 'Oxford Linear Regression Slope'
    description       = 'Dual linear-regression-slope trend filter on liquid ETFs (oxfordstrat linear-regression). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 110
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'lb': 100, 'lb2': 50}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        lb, lb2 = int(p['lb']), int(p['lb2'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            cseries = bars['close']
            if len(cseries) < lb + 1:
                continue
            close = float(cseries.iloc[-1])
            s1 = linreg_slope(cseries, lb)
            s2 = linreg_slope(cseries, lb2)
            if s1 != s1 or s2 != s2 or close <= 0:
                continue
            # Normalize the slope by price so the ranking edge is comparable across ETFs.
            if s1 > 0 and s2 > 0:
                direction, edge = 'LONG', (s1 / close)
            elif s1 < 0 and s2 < 0:
                direction, edge = 'SHORT', (-s1 / close)
            else:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence='MED',
                signal_params={'lb': lb, 'lb2': lb2, 'slope_per_price': round(float(edge), 6),
                               'regime': regime_state, 'source': 'oxfordstrat:linear-regression'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
