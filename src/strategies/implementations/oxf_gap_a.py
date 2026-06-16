"""Gap Pattern Type A (oxfordstrat.com/trading-strategies/gap-pattern/) — full-gap
continuation in the direction of the trend.

CONFIRMED-BREAKOUT DAILY-BAR ADAPTATION: Oxford's rule is a buy AT the open after
a full gap up (Low[i] > High[i-1]) when the trend filter is bullish (High[i] >
UpperChannel[i-1], the highest high over Filter_Look_Back). The backtest engine
fills at close[t+1], not at the gap-open, so we model the CONFIRMED pattern: when
the full gap + trend filter hold on signal-day t, emit a directional signal; the
engine enters at close[t+1]. Short = mirror (High[i] < Low[i-1] and Low[i] <
LowerChannel[i-1]). This is a confirmed-pattern daily-bar adaptation, NOT an
at-the-open fill.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, gap_dir, donchian_prev, atr

__all__ = ['OxfGapA']


class OxfGapA(OxfordBaseStrategy):
    id                = 'oxf_gap_a'
    name              = 'Oxford Gap Pattern A Confirmed Continuation (adaptation)'
    description       = ('Full-gap continuation in trend direction on liquid ETFs — confirmed '
                         'daily-bar adaptation (oxfordstrat gap-pattern). Full gap + Donchian trend '
                         'filter confirmed on signal-day OHLC; close[t+1] fill.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 40
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'filter_lb': 20}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        flb = int(p['filter_lb'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < flb + 2:
                continue
            g = gap_dir(bars)  # +1 full gap up, -1 full gap down, 0 none
            if g == 0:
                continue
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            # Trend filter (page): High[i] > UpperChannel[i-1] for longs, where
            # UpperChannel = highest high over Filter_Look_Back PRIOR bars.
            up_ch, lo_ch = donchian_prev(bars, flb)
            if up_ch != up_ch or lo_ch != lo_ch:
                continue
            today = bars.iloc[-1]
            close_now = float(today['close'])
            direction, edge = None, 0.0
            if g == 1 and float(today['high']) > up_ch:
                gap_size = float(today['low']) - float(bars['high'].iloc[-2])
                direction, edge = 'LONG', gap_size / a
            elif g == -1 and float(today['low']) < lo_ch:
                gap_size = float(bars['low'].iloc[-2]) - float(today['high'])
                direction, edge = 'SHORT', gap_size / a
            if direction is None:
                continue
            ranked.append((edge, t, direction, close_now, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close_now, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close_now, regime_state=regime_state)
            conf = 'HIGH' if edge >= 1.0 else 'MED' if edge >= 0.3 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close_now,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'filter_lb': flb, 'gap_atr': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:gap-pattern',
                               'note': 'confirmed-breakout daily-bar adaptation'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
