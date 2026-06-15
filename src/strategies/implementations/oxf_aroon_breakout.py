"""Aroon indicator breakout (oxfordstrat.com/trading-strategies/aroon-indicator-breakout-1/).
Faithful daily-bar rule: LONG when AroonUp > AroonDown and AroonUp >= Min_Aroon;
SHORT when AroonDown > AroonUp and AroonDown >= Min_Aroon (symmetric — Oxford has
both). AroonUp = 100*(n - bars_since_highest_high_in_(n+1))/n. Condition evaluated
at the last bar (Oxford's [i-1]→open[i]; the engine fills at close[t+1]).
ETF-basket cross-section; house ATR-multiple brackets.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, aroon, atr

__all__ = ['OxfAroonBreakout']


class OxfAroonBreakout(OxfordBaseStrategy):
    id                = 'oxf_aroon_breakout'
    name              = 'Oxford Aroon Indicator Breakout'
    description       = 'Aroon up/down breakout on liquid ETFs (oxfordstrat aroon-indicator-breakout-1). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 40
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'aroon_len': 25, 'min_aroon': 70.0}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        n = int(p['aroon_len'])
        min_aroon = float(p['min_aroon'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < n + 2:
                continue
            up, dn = aroon(bars, n)
            if up != up or dn != dn:
                continue
            close = float(bars['close'].iloc[-1])
            if up > dn and up >= min_aroon:
                direction, strength = 'LONG', up
            elif dn > up and dn >= min_aroon:
                direction, strength = 'SHORT', dn
            else:
                continue
            ranked.append((strength, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for strength, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if strength >= 95.0 else 'MED'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'aroon_len': n, 'aroon_strength': round(float(strength), 2),
                               'regime': regime_state,
                               'source': 'oxfordstrat:aroon-indicator-breakout-1'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
