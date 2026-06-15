"""Dow Theory trend (oxfordstrat.com/trading-strategies/dow-theory-trend/).
Faithful daily-bar rule: LONG when close breaks above the most-recent confirmed
swing-pivot high (a higher-high confirms an up-trend); SHORT when close breaks below
the most-recent confirmed swing-pivot low (lower-low confirms a down-trend). Oxford
defines pivots via DeMark Pivot_Size = number of higher/lower bars on EACH side
("a low that has two higher lows on either side has Pivot_Size = 2"), implemented by
the general fractal swing_pivots(k=pivot_size). Condition at the last bar (Oxford's
[i-1]→open[i]; engine fills close[t+1]). ETF-basket; house ATR brackets.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, swing_pivots, atr

__all__ = ['OxfDowTheory']


class OxfDowTheory(OxfordBaseStrategy):
    id                = 'oxf_dow_theory'
    name              = 'Oxford Dow Theory Trend'
    description       = 'Dow-theory swing-pivot breakout on liquid ETFs (oxfordstrat dow-theory-trend). Close breaks the most-recent pivot. Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 60
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'pivot_size': 10}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        k = int(p['pivot_size'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < 2 * k + 2:
                continue
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            highs, lows = swing_pivots(bars, k)
            if not highs and not lows:
                continue
            close = float(bars['close'].iloc[-1])
            direction = None
            edge = 0.0
            recent_high = highs[-1][1] if highs else None
            recent_low = lows[-1][1] if lows else None
            if recent_high is not None and close > recent_high:
                direction, edge = 'LONG', (close - recent_high) / a
            elif recent_low is not None and close < recent_low:
                direction, edge = 'SHORT', (recent_low - close) / a
            if direction is None:
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
                signal_params={'pivot_size': k, 'breakout_atr': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:dow-theory-trend'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
