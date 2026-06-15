"""Fractal Adaptive Moving Average (oxfordstrat.com/trading-strategies/fractal-adaptive-moving-average/).
Faithful daily-bar rule: long when close > FRAMA + band*ATR; short = mirror
(close < FRAMA - band*ATR). Oxford cites Ehlers without restating the formula, so
the standard Ehlers FRAMA is used (Price=(High+Low)/2, fractal-dimension alpha).
Condition evaluated at close[t]; engine fills at close[t+1]. House brackets.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, frama, atr

__all__ = ['OxfFrama']


class OxfFrama(OxfordBaseStrategy):
    id                = 'oxf_frama'
    name              = 'Oxford Fractal Adaptive MA'
    description       = 'FRAMA (Ehlers) volatility-band breakout on liquid ETFs (oxfordstrat fractal-adaptive-moving-average). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 70
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'frama_len': 40, 'band': 1.0, 'atr_len': 20}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        flen = int(p['frama_len']); band = float(p['band']); alen = int(p['atr_len'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < flen + 2:
                continue
            f = frama(bars, flen)
            a = atr(bars, alen)
            close = float(bars['close'].iloc[-1])
            if f != f or a is None or a != a or a <= 0:
                continue
            upper = f + band * a
            lower = f - band * a
            if close > upper:
                direction, edge = 'LONG', (close - upper) / a
            elif close < lower:
                direction, edge = 'SHORT', (lower - close) / a
            else:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if edge >= 1.0 else 'MED' if edge >= 0.3 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'frama_len': flen, 'band_atr': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:fractal-adaptive-moving-average'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
