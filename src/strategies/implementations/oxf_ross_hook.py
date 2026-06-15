"""Ross Hook Filter 2 (oxfordstrat.com/trading-strategies/ross-hook-filter-2/) —
Joe Ross' 1-2-3 trend break followed by a Ross-hook breakout entry.

CONFIRMED-BREAKOUT DAILY-BAR ADAPTATION: Oxford's pattern is a 1-2-3 formation
(point 1 = a swing extreme, point 2 = the reaction extreme, point 3 = the trend
break) of minimum size Min_Setup bars; the Ross hook is "the first pullback
correction following a 1-2-3 breakout", filtered by Hook <= (Drawdown * Max_Hook);
the ENTRY is an intraday buy stop one tick above the highest high of the Ross hook
(sell stop below the lowest low for shorts). The backtest engine fills at
close[t+1] and cannot honor an intraday stop, so we model the CONFIRMED breakout:
once a valid 1-2-3 + hook structure exists and High[t] pierces the hook high on
signal-day t, emit a directional signal; the engine enters at close[t+1].

We detect the 1-2-3 swing structure with the general fractal swing_pivots (k=2)
helper (the page does not specify the local-extreme window — documented choice,
same as oxf_livermore/oxf_dow_theory). LONG structure: the two most-recent
confirmed pivots are a swing low (point 1/2 reaction) then a HIGHER swing high =
the Ross hook; the pattern span must be >= Min_Setup and the hook pullback within
Max_Hook of the prior drawdown. Short = mirror. Confirmed-breakout adaptation,
NOT an intraday stop fill.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, swing_pivots, atr

__all__ = ['OxfRossHook']


class OxfRossHook(OxfordBaseStrategy):
    id                = 'oxf_ross_hook'
    name              = 'Oxford Ross Hook Confirmed Breakout (adaptation)'
    description       = ('Ross hook 1-2-3 trend-break breakout on liquid ETFs — confirmed-breakout '
                         'daily-bar adaptation (oxfordstrat ross-hook-filter-2). Hook high pierced on '
                         'signal-day OHLC after a 1-2-3 swing structure; close[t+1] fill.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 60
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'pivot_k': 2, 'min_setup': 10, 'max_hook': 1.0}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        k = int(p['pivot_k'])
        min_setup = int(p['min_setup'])
        max_hook = float(p['max_hook'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < min_setup + 4 * k + 4:
                continue
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            highs, lows = swing_pivots(bars, k)
            if len(highs) < 2 or len(lows) < 2:
                continue
            close_now = float(bars['close'].iloc[-1])
            high_now = float(bars['high'].iloc[-1])
            low_now = float(bars['low'].iloc[-1])
            direction, edge = None, 0.0
            # --- LONG: ...low (pt1) -> higher high (pt2) -> higher low (pt3=hook) ---
            # Ross hook = the most-recent confirmed swing HIGH after an up-leg; the
            # pullback low (most-recent swing low) sits above the prior swing low.
            last_high_idx, last_high = highs[-1]
            prev_low_idx, prev_low = lows[-1]
            if len(lows) >= 2:
                older_low_idx, older_low = lows[-2]
                # Up-structure: rising lows (higher-low pullback) + a swing high above.
                up_struct = (prev_low > older_low and last_high > older_low
                             and (last_high_idx - older_low_idx) >= min_setup)
                if up_struct:
                    drawdown = last_high - older_low
                    hook_pullback = last_high - prev_low
                    if drawdown > 0 and hook_pullback <= drawdown * max_hook:
                        if high_now >= last_high:  # confirmed pierce of hook high
                            direction, edge = 'LONG', (high_now - last_high) / a
            # --- SHORT mirror: falling highs + a swing low below; pierce hook low ---
            if direction is None and len(highs) >= 2:
                last_low_idx, last_low = lows[-1]
                older_high_idx, older_high = highs[-2]
                recent_high_idx, recent_high = highs[-1]
                down_struct = (recent_high < older_high and last_low < older_high
                               and (last_low_idx - older_high_idx) >= min_setup)
                if down_struct:
                    rally = older_high - last_low
                    hook_pullback = recent_high - last_low
                    if rally > 0 and hook_pullback <= rally * max_hook:
                        if low_now <= last_low:
                            direction, edge = 'SHORT', (last_low - low_now) / a
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
                signal_params={'min_setup': min_setup, 'max_hook': max_hook,
                               'trigger_atr': round(float(edge), 3), 'regime': regime_state,
                               'source': 'oxfordstrat:ross-hook-filter-2',
                               'note': 'confirmed-breakout daily-bar adaptation'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
