"""ORBP trend (oxfordstrat.com/trading-strategies/orbp-trend/) — Toby Crabel's
Opening-Range-Breakout-Preference with a momentum trend filter.

CONFIRMED-BREAKOUT DAILY-BAR ADAPTATION: Oxford rests an intraday buy stop at
[Open + Stretch] (and a sell stop at [Open − Stretch]) when the trend filter is
bullish/bearish; Stretch = Average_Noise(10) * Stretch_Multiple, Noise =
min(High−Open, Open−Low). The backtest engine fills at close[t+1] and cannot
honor an intraday stop, so we model the CONFIRMED breakout: on day t whose
high pierced Open+Stretch (long) — the stop WOULD have triggered intraday — and
the close confirms the direction, emit a directional signal; the engine enters
at close[t+1]. Trend filter (page): Close[i] > Close[i − Filter_Look_Back + 1]
for longs. This is a confirmed-breakout daily-bar adaptation, NOT a tick-exact
replica of the intraday stop fill.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, avg_noise

__all__ = ['OxfOrbpMomentum']


class OxfOrbpMomentum(OxfordBaseStrategy):
    id                = 'oxf_orbp_momentum'
    name              = 'Oxford ORBP Trend Confirmed Breakout (adaptation)'
    description       = ('ORBP opening-range-breakout-preference with momentum trend filter on '
                         'liquid ETFs — confirmed-breakout daily-bar adaptation (oxfordstrat orbp-trend). '
                         'Buy stop at Open+Stretch confirmed on signal-day OHLC; close[t+1] fill.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 40
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'filter_lb': 20, 'stretch_lookback': 10, 'stretch_mult': 1.0}

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
            stretch = avg_noise(bars, int(p['stretch_lookback'])) * float(p['stretch_mult'])
            if stretch != stretch or stretch <= 0:
                continue
            close_now = float(bars['close'].iloc[-1])
            # Trend filter (page): Close[i] > Close[i - Filter_Look_Back + 1].
            ref_close = float(bars['close'].iloc[-flb])  # i - (flb-1) bars back
            today = bars.iloc[-1]
            o = float(today['open'])
            up_trig = o + stretch
            dn_trig = o - stretch
            direction, edge = None, 0.0
            if close_now > ref_close:  # bullish trend
                if float(today['high']) >= up_trig and close_now >= o:
                    direction, edge = 'LONG', (float(today['high']) - up_trig) / stretch
            elif close_now < ref_close:  # bearish trend
                if float(today['low']) <= dn_trig and close_now <= o:
                    direction, edge = 'SHORT', (dn_trig - float(today['low'])) / stretch
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
                signal_params={'filter_lb': flb, 'trigger_edge': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:orbp-trend',
                               'note': 'confirmed-breakout daily-bar adaptation'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
