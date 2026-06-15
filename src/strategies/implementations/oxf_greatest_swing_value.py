"""Greatest Swing Value trend (oxfordstrat.com/trading-strategies/greatest-swing-value-trend/).
Crabel's GSV volatility-breakout with a momentum trend filter.

CONFIRMED-BREAKOUT DAILY-BAR ADAPTATION: Oxford rests an intraday buy stop at
[Open + GSV] (sell stop at [Open − GSV]) when the trend is bullish/bearish, where
GSV = Average_Noise(GSV_Length) * GSV_Multiple and the DIRECTIONAL noise is
open-low on up days / high-open on down days (see gsv() helper). The backtest
engine fills at close[t+1] and cannot honor an intraday stop, so we model the
CONFIRMED breakout: on signal-day t whose high pierced Open+GSV (long) with the
close confirming the direction, emit a directional signal; the engine enters at
close[t+1]. Trend filter: Close[i] > Close[i − filter_lb] (momentum). This is a
confirmed-breakout daily-bar adaptation, NOT a tick-exact intraday stop fill.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, gsv

__all__ = ['OxfGreatestSwingValue']


class OxfGreatestSwingValue(OxfordBaseStrategy):
    id                = 'oxf_greatest_swing_value'
    name              = 'Oxford Greatest Swing Value Confirmed Breakout (adaptation)'
    description       = ('GSV greatest-swing-value volatility breakout with trend filter on liquid '
                         'ETFs — confirmed-breakout daily-bar adaptation (oxfordstrat '
                         'greatest-swing-value-trend). Buy stop at Open+GSV confirmed on signal-day '
                         'OHLC; close[t+1] fill.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 40
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'gsv_lb': 10, 'mult': 2.0, 'filter_lb': 20}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        gsv_lb = int(p['gsv_lb'])
        mult = float(p['mult'])
        flb = int(p['filter_lb'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < max(gsv_lb, flb) + 2:
                continue
            base = gsv(bars, gsv_lb)            # Average_Noise; mult applied below
            if base != base or base <= 0:
                continue
            gsv_val = base * mult
            close_now = float(bars['close'].iloc[-1])
            ref_close = float(bars['close'].iloc[-1 - flb])  # close flb bars ago
            today = bars.iloc[-1]
            o = float(today['open'])
            up_trig = o + gsv_val
            dn_trig = o - gsv_val
            direction, edge = None, 0.0
            if close_now > ref_close:  # bullish trend
                if float(today['high']) >= up_trig and close_now >= o:
                    direction, edge = 'LONG', (float(today['high']) - up_trig) / gsv_val
            elif close_now < ref_close:  # bearish trend
                if float(today['low']) <= dn_trig and close_now <= o:
                    direction, edge = 'SHORT', (dn_trig - float(today['low'])) / gsv_val
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
                signal_params={'gsv_lb': gsv_lb, 'mult': mult, 'trigger_edge': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:greatest-swing-value-trend',
                               'note': 'confirmed-breakout daily-bar adaptation'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
