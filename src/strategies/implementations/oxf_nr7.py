"""NR7 narrow-range breakout (oxfordstrat.com/trading-strategies/nr7/).
DAILY-BAR ADAPTATION: the Crabel rule rests an intraday stop at Open±Stretch on
the bar after an NR7. The backtest engine fills at close[t+1] and cannot honor an
intraday stop, so we model a CONFIRMED breakout: on an NR7 bar t whose close
pierced Open±Stretch (the stop would have triggered intraday), emit a directional
signal; the engine enters at close[t+1]. This is a confirmed-breakout adaptation,
NOT a tick-exact replica of the intraday stop fill.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, is_nrn, avg_noise

__all__ = ['OxfNR7']


class OxfNR7(OxfordBaseStrategy):
    id                = 'oxf_nr7'
    name              = 'Oxford NR7 Confirmed Breakout (adaptation)'
    description       = 'NR7 narrow-range breakout on liquid ETFs — daily-bar confirmed-breakout adaptation (oxfordstrat nr7). close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'nr_length': 7, 'stretch_lookback': 10, 'stretch_mult': 1.0}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        nrlen = int(p['nr_length'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < nrlen + 2:
                continue
            if not is_nrn(bars, nrlen):
                continue
            stretch = avg_noise(bars, int(p['stretch_lookback'])) * float(p['stretch_mult'])
            if stretch != stretch or stretch <= 0:
                continue
            today = bars.iloc[-1]
            up_trig = float(today['open']) + stretch
            dn_trig = float(today['open']) - stretch
            close = float(today['close'])
            # Confirmed intraday trigger: the day's high/low pierced the stop level.
            if float(today['high']) >= up_trig and close >= float(today['open']):
                direction, edge = 'LONG', (float(today['high']) - up_trig) / stretch
            elif float(today['low']) <= dn_trig and close <= float(today['open']):
                direction, edge = 'SHORT', (dn_trig - float(today['low'])) / stretch
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
                position_size_pct=round((1.0/max(len(keep),1))*0.18*scale, 4),
                confidence='MED',
                signal_params={'nr_length': nrlen, 'trigger_edge': round(float(edge),3),
                               'regime': regime_state, 'source': 'oxfordstrat:nr7',
                               'note': 'confirmed-breakout daily-bar adaptation'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
