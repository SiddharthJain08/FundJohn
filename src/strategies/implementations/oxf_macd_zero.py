"""MACD zero-line trend (oxfordstrat.com/trading-strategies/macd-part-1/).
Faithful daily-bar rule: long when the MACD line (EMA(fast) - EMA(slow)) > 0;
short when MACD line < 0. Condition at close[t]; engine fills at close[t+1].
House brackets. ETF-basket cross-section.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, macd

__all__ = ['OxfMacdZero']


class OxfMacdZero(OxfordBaseStrategy):
    id                = 'oxf_macd_zero'
    name              = 'Oxford MACD Zero-Line'
    description       = 'MACD-line zero-cross trend filter on liquid ETFs (oxfordstrat macd-part-1). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 40
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'ema_fast': 12, 'ema_slow': 26}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        fast, slow = int(p['ema_fast']), int(p['ema_slow'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            cseries = bars['close']
            if len(cseries) < slow + 2:
                continue
            close = float(cseries.iloc[-1])
            line = macd(cseries, fast, slow)
            if line != line or close <= 0:
                continue
            if line > 0:
                direction, edge = 'LONG', (line / close)
            elif line < 0:
                direction, edge = 'SHORT', (-line / close)
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
                signal_params={'ema_fast': fast, 'ema_slow': slow, 'macd_per_price': round(float(edge), 6),
                               'regime': regime_state, 'source': 'oxfordstrat:macd-part-1'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
