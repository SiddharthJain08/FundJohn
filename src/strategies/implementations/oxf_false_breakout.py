"""False Breakout 1 (oxfordstrat.com/trading-strategies/false-breakout-1/) — fade
a failed channel breakout (buy a failed DOWNSIDE breakout; short = mirror).

PLAN-vs-PAGE DIVERGENCE (resolved in favor of the cited page, per the fidelity
mandate): the plan table glossed this as "prior N-day HIGH broken... close back
inside (fade)" with a second channel ch2=10. The actual oxfordstrat page is a
single-channel DOWNSIDE false breakout that you BUY:
  Setup: the market makes a new Channel_#1-bar LOW (new breakout bar); the
         previous Channel_#1-bar low (old breakout bar) was made at least 5 bars
         earlier; the close of the new breakout bar is at or below the old low.
  Entry: within fewer than 5 bars after the setup, a buy stop one tick above the
         HIGH of the new breakout bar.
There is NO second channel — ch2 is dropped. Single param Channel_#1 (default 40).

CONFIRMED-BREAKOUT DAILY-BAR ADAPTATION: the buy stop is intraday; the engine
fills at close[t+1]. We model the CONFIRMED entry: on signal-day t, once a valid
false-breakdown setup exists within the last <5 bars and High[t] has pierced the
new-breakout-bar high (the buy-stop level), emit a directional signal; the engine
enters at close[t+1]. Short = the mirror failed-UPSIDE breakout.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, atr

__all__ = ['OxfFalseBreakout']


class OxfFalseBreakout(OxfordBaseStrategy):
    id                = 'oxf_false_breakout'
    name              = 'Oxford False Breakout Fade Confirmed (adaptation)'
    description       = ('Failed channel-breakout fade on liquid ETFs — confirmed-breakout daily-bar '
                         'adaptation (oxfordstrat false-breakout-1). Buy a failed downside breakout: '
                         'new N-bar low with the prior N-bar low >=5 bars earlier, then a confirmed '
                         'recovery pierce on signal-day OHLC; close[t+1] fill. Page-faithful (single '
                         'channel, drops plan ch2).')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 60
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'channel': 40, 'min_separation': 5, 'entry_window': 5}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        ch = int(p['channel'])
        sep = int(p['min_separation'])
        win = int(p['entry_window'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < ch + win + sep + 3:
                continue
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            lows = bars['low'].to_numpy(dtype=float)
            highs = bars['high'].to_numpy(dtype=float)
            closes = bars['close'].to_numpy(dtype=float)
            m = len(lows)
            close_now = float(closes[-1])
            high_now = float(highs[-1])
            low_now = float(lows[-1])
            direction, edge = None, 0.0
            # --- BUY a failed DOWNSIDE breakout ---
            # Scan candidate new-breakout bars in the last `win` bars (excluding t,
            # which is the trigger bar). j = positional index of the new-low bar.
            for j in range(m - 2, m - 2 - win, -1):
                if j < ch + sep:
                    break
                # j is a new Channel-bar low: its low is the min over the prior ch bars.
                prior_window = lows[j - ch:j]
                if lows[j] > prior_window.min():
                    continue
                old_low = float(prior_window.min())
                # old breakout bar (the prior ch-bar low) must be >= sep bars earlier.
                # positional index of the most-recent occurrence of old_low within prior_window:
                old_idx = j - ch + int((prior_window == prior_window.min()).nonzero()[0][-1])
                if (j - old_idx) < sep:
                    continue
                # close of the new breakout bar at/below the old low (failed deeper).
                if closes[j] > old_low + 1e-9:
                    continue
                # confirmed recovery: today's high pierced the new-breakout-bar high.
                if high_now >= highs[j]:
                    direction, edge = 'LONG', (high_now - highs[j]) / a + 0.01
                    break
            # --- SHORT the mirror (failed UPSIDE breakout) ---
            if direction is None:
                for j in range(m - 2, m - 2 - win, -1):
                    if j < ch + sep:
                        break
                    prior_window = highs[j - ch:j]
                    if highs[j] < prior_window.max():
                        continue
                    old_high = float(prior_window.max())
                    old_idx = j - ch + int((prior_window == prior_window.max()).nonzero()[0][-1])
                    if (j - old_idx) < sep:
                        continue
                    if closes[j] < old_high - 1e-9:
                        continue
                    if low_now <= lows[j]:
                        direction, edge = 'SHORT', (lows[j] - low_now) / a + 0.01
                        break
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
                signal_params={'channel': ch, 'recovery_atr': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:false-breakout-1',
                               'note': 'confirmed-breakout daily-bar adaptation (page-faithful, single channel)'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
