"""Bull Oops Pattern (oxfordstrat.com/trading-strategies/bull-oops-pattern/) —
Larry Williams' "Oops!" gap-fade reversal (bull/long variant).

CONFIRMED-BREAKOUT DAILY-BAR ADAPTATION: Oxford's setup is Open[today] <
Low[yesterday] (today gaps open below yesterday's low); the entry is an intraday
buy stop at Low[yesterday] — price recovers back up THROUGH the prior low. The
backtest engine fills at close[t+1] and cannot honor an intraday stop, so we
model the CONFIRMED recovery: on signal-day t that gapped open below Low[t-1] and
whose high then pierced Low[t-1] (the buy-stop level was hit), emit a directional
signal; the engine enters at close[t+1]. Short = the bear-Oops mirror (Open >
High[t-1], low pierces High[t-1]). ATR_Index (default 1.0) is the page's EXIT
sizing parameter, carried as documentation only. This is a confirmed-breakout
daily-bar adaptation, NOT an intraday stop fill.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, atr

__all__ = ['OxfBullOops']


class OxfBullOops(OxfordBaseStrategy):
    id                = 'oxf_bull_oops'
    name              = 'Oxford Bull Oops Confirmed Reversal (adaptation)'
    description       = ('Bull "Oops!" gap-fade reversal on liquid ETFs — confirmed-breakout daily-bar '
                         'adaptation (oxfordstrat bull-oops-pattern). Gap open below prior low then '
                         'recovery through prior low confirmed on signal-day OHLC; close[t+1] fill.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'atr_idx': 1.0}  # page EXIT parameter; documentation only here

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < 25:
                continue
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            today = bars.iloc[-1]        # bar t (recovery/trigger bar)
            y = bars.iloc[-2]            # bar t-1
            o = float(today['open'])
            close_now = float(today['close'])
            prev_low = float(y['low'])
            prev_high = float(y['high'])
            direction, edge = None, 0.0
            # BULL Oops: gap open below prior low, recover up through prior low.
            if o < prev_low and float(today['high']) >= prev_low:
                direction, edge = 'LONG', (float(today['high']) - prev_low) / a
            # BEAR Oops mirror: gap open above prior high, fall back through prior high.
            elif o > prev_high and float(today['low']) <= prev_high:
                direction, edge = 'SHORT', (prev_high - float(today['low'])) / a
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
                signal_params={'recovery_atr': round(float(edge), 3), 'regime': regime_state,
                               'source': 'oxfordstrat:bull-oops-pattern',
                               'note': 'confirmed-breakout daily-bar adaptation'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
