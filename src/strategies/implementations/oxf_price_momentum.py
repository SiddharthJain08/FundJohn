"""Price-momentum model (oxfordstrat.com/trading-strategies/price-momentum-model/).
Faithful daily-bar rule: long when mom(n1) > 0 AND mom(n2) > 0 where
mom(lag) = close - close[lag]; short when both < 0. Condition at close[t]; engine
fills at close[t+1]. House brackets. ETF-basket cross-section.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy

__all__ = ['OxfPriceMomentum']


class OxfPriceMomentum(OxfordBaseStrategy):
    id                = 'oxf_price_momentum'
    name              = 'Oxford Price Momentum'
    description       = 'Dual absolute price-momentum (close minus close-lag) trend filter on liquid ETFs (oxfordstrat price-momentum-model). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 110
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'n1': 100, 'n2': 50}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        n1, n2 = int(p['n1']), int(p['n2'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            cseries = bars['close']
            if len(cseries) < n1 + 1:
                continue
            close = float(cseries.iloc[-1])
            if close <= 0:
                continue
            mom1 = close - float(cseries.iloc[-n1 - 1])
            mom2 = close - float(cseries.iloc[-n2 - 1])
            if mom1 != mom1 or mom2 != mom2:
                continue
            if mom1 > 0 and mom2 > 0:
                direction, edge = 'LONG', (mom1 / close)
            elif mom1 < 0 and mom2 < 0:
                direction, edge = 'SHORT', (-mom1 / close)
            else:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if edge >= 0.15 else 'MED' if edge >= 0.05 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'n1': n1, 'n2': n2, 'mom_per_price': round(float(edge), 4),
                               'regime': regime_state, 'source': 'oxfordstrat:price-momentum-model'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
