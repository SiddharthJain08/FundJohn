"""Crabel Hook pattern (oxfordstrat.com/trading-strategies/pattern-hook/) — a
gap-and-narrowing-range setup followed by an opening-range-breakout entry.

CONFIRMED-BREAKOUT DAILY-BAR ADAPTATION: Oxford's hook SETUP (long) is a bar that
"opens above the previous day's high and closes below the previous day's close
with a narrowing range"; the ENTRY is an intraday buy stop at [Open + Stretch] on
the NEXT bar (Stretch = Average_Noise(10) * Stretch_Multiple, min-shadow noise).
The backtest engine fills at close[t+1] and cannot honor an intraday stop, so we
model it faithfully across two bars: the hook setup forms on bar t-1 and the
breakout TRIGGER is confirmed on bar t when High[t] pierces Open[t] + Stretch;
emit a directional signal, engine enters at close[t+1]. Short = mirror (open <
prior low, close > prior close, narrowing; Low[t] <= Open[t] − Stretch).

NOTE: there is deliberately NO `close >= open` confirmation here — the hook's
own close condition lives on the SETUP bar (t-1) and is opposite to a same-day
breakout-close filter; imposing one would make the rule emit zero signals. The
"confirmed" part is the trigger-level pierce on bar t.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, avg_noise

__all__ = ['OxfHook']


class OxfHook(OxfordBaseStrategy):
    id                = 'oxf_hook'
    name              = 'Oxford Crabel Hook Confirmed Breakout (adaptation)'
    description       = ('Crabel hook gap-and-narrowing-range ORB on liquid ETFs — confirmed-breakout '
                         'daily-bar adaptation (oxfordstrat pattern-hook). Hook setup on t-1, '
                         'Open+Stretch pierce confirmed on signal-day t; close[t+1] fill.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'stretch_lookback': 10, 'stretch_mult': 2.0}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < int(p['stretch_lookback']) + 4:
                continue
            today = bars.iloc[-1]        # bar t (trigger bar)
            hook = bars.iloc[-2]         # bar t-1 (hook setup bar)
            prev = bars.iloc[-3]         # bar t-2 (reference for the gap/close)
            # Stretch computed on the hook setup bar (its open is the ORB anchor for
            # the next-bar stop in Oxford). Use the noise window ending at t-1.
            stretch = avg_noise(bars.iloc[:-1], int(p['stretch_lookback'])) * float(p['stretch_mult'])
            if stretch != stretch or stretch <= 0:
                continue
            hook_range = float(hook['high']) - float(hook['low'])
            prev_range = float(prev['high']) - float(prev['low'])
            narrowing = hook_range < prev_range
            close_now = float(today['close'])
            o_t = float(today['open'])
            up_trig = o_t + stretch
            dn_trig = o_t - stretch
            direction, edge = None, 0.0
            # LONG hook setup: open above prior high, close below prior close, narrowing.
            long_hook = (float(hook['open']) > float(prev['high'])
                         and float(hook['close']) < float(prev['close']) and narrowing)
            # SHORT hook setup: open below prior low, close above prior close, narrowing.
            short_hook = (float(hook['open']) < float(prev['low'])
                          and float(hook['close']) > float(prev['close']) and narrowing)
            if long_hook and float(today['high']) >= up_trig:
                direction, edge = 'LONG', (float(today['high']) - up_trig) / stretch
            elif short_hook and float(today['low']) <= dn_trig:
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
                signal_params={'stretch_mult': float(p['stretch_mult']),
                               'trigger_edge': round(float(edge), 3), 'regime': regime_state,
                               'source': 'oxfordstrat:pattern-hook',
                               'note': 'confirmed-breakout daily-bar adaptation'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
