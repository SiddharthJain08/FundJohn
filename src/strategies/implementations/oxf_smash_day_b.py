"""Smash Day Pattern B1 (oxfordstrat.com/trading-strategies/smash-day-pattern-b1/) —
Larry Williams' smash-day reversal (buy variant).

CONFIRMED-BREAKOUT DAILY-BAR ADAPTATION: Oxford's setup is a "smash day" on the
prior bar — Close[i-1] < Low[i-2] (yesterday closed below the low of 2 days ago) —
plus an uptrend filter Close[i-1] > Close[i-1 − Trend_Index]; the entry is an
intraday buy stop one tick above High[i-1]. The backtest engine fills at
close[t+1] and cannot honor an intraday stop, so we model the CONFIRMED reversal:
on signal-day t whose high pierced High[t-1] (the buy-stop level) after a valid
smash setup on t-1 and a bullish trend, emit a directional signal; the engine
enters at close[t+1]. Short = mirror (Close[i-1] > High[i-2], downtrend, low <
Low[t-1]). This is a confirmed-breakout daily-bar adaptation, NOT an intraday
stop fill.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, atr

__all__ = ['OxfSmashDayB']


class OxfSmashDayB(OxfordBaseStrategy):
    id                = 'oxf_smash_day_b'
    name              = 'Oxford Smash Day B1 Confirmed Reversal (adaptation)'
    description       = ('Smash-day B1 reversal on liquid ETFs — confirmed-breakout daily-bar '
                         'adaptation (oxfordstrat smash-day-pattern-b1). Prior-bar smash setup + trend '
                         'filter, prior-high pierce confirmed on signal-day OHLC; close[t+1] fill.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 50
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'trend_idx': 40}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        ti = int(p['trend_idx'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < ti + 3:
                continue
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            today = bars.iloc[-1]        # bar t (trigger bar)
            y = bars.iloc[-2]            # bar t-1 (smash setup bar)
            yy = bars.iloc[-3]           # bar t-2
            # Trend ref: Close[t-1] vs Close[t-1 - ti].
            trend_ref = float(bars['close'].iloc[-2 - ti])
            cy = float(y['close'])
            close_now = float(today['close'])
            direction, edge = None, 0.0
            # BUY smash: yesterday closed below the low of 2 days ago, uptrend,
            # today's high pierces yesterday's high (buy stop triggered).
            if cy < float(yy['low']) and cy > trend_ref:
                if float(today['high']) >= float(y['high']):
                    direction, edge = 'LONG', (float(today['high']) - float(y['high'])) / a
            # SELL smash: yesterday closed above the high of 2 days ago, downtrend,
            # today's low pierces yesterday's low (sell stop triggered).
            elif cy > float(yy['high']) and cy < trend_ref:
                if float(today['low']) <= float(y['low']):
                    direction, edge = 'SHORT', (float(y['low']) - float(today['low'])) / a
            if direction is None:
                continue
            ranked.append((edge, t, direction, close_now, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close_now, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close_now, regime_state=regime_state)
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close_now,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence='MED',
                signal_params={'trend_idx': ti, 'trigger_atr': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:smash-day-pattern-b1',
                               'note': 'confirmed-breakout daily-bar adaptation'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
