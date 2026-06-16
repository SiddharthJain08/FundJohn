"""Heikin-Ashi candle reversal (oxfordstrat.com/trading-strategies/heikin-ashi-1/).
Faithful daily-bar rule: LONG when the latest Heikin-Ashi candle is bullish
(HaClose > HaOpen); SHORT when bearish (HaClose < HaOpen) — Oxford's single-bar
reversal setup (NO consecutive-bar requirement is on the page). Oxford first smooths
the OHLC with a Look_Back SMA before the HA transform; the page DEFAULT Look_Back=1
(= raw OHLC = standard Heikin-Ashi), which is the default here — the indicator
supports >1 faithfully (heikin_ashi_series look_back). Consecutive-bullish (or
-bearish) HA run length is used ONLY to rank for MAX_SIGNALS (trend persistence),
never as the entry gate. Condition evaluated at the last bar (Oxford's [i-1]→open[i];
the engine fills at close[t+1]). ETF-basket; house brackets.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, heikin_ashi_series

__all__ = ['OxfHeikinAshi']


def _ha_run_len(ha_open, ha_close, bullish: bool) -> int:
    """Length of the consecutive run of bullish (or bearish) HA candles ending now."""
    n = len(ha_close)
    run = 0
    for i in range(n - 1, -1, -1):
        is_bull = ha_close.iloc[i] > ha_open.iloc[i]
        if is_bull == bullish:
            run += 1
        else:
            break
    return run


class OxfHeikinAshi(OxfordBaseStrategy):
    id                = 'oxf_heikin_ashi'
    name              = 'Oxford Heikin-Ashi Candle Reversal'
    description       = 'Heikin-Ashi single-bar candle reversal on liquid ETFs (oxfordstrat heikin-ashi-1). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        # look_back = Oxford's pre-HA OHLC smoothing window (page default 1 = raw HA).
        return {'look_back': 1}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        look_back = int(self.parameters.get('look_back', 1))
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < 3:
                continue
            ha = heikin_ashi_series(bars, look_back=look_back)
            ha_open, ha_close = ha['ha_open'], ha['ha_close']
            o = float(ha_open.iloc[-1])
            c = float(ha_close.iloc[-1])
            if c != c or o != o or c == o:
                continue
            if c > o:
                direction = 'LONG'
                run = _ha_run_len(ha_open, ha_close, bullish=True)
            else:
                direction = 'SHORT'
                run = _ha_run_len(ha_open, ha_close, bullish=False)
            close = float(bars['close'].iloc[-1])
            ranked.append((run, t, direction, close, bars))
        ranked.sort(reverse=True)  # longer persistence ranks first
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for run, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if run >= 4 else 'MED' if run >= 2 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'ha_run_len': int(run), 'look_back': look_back,
                               'regime': regime_state,
                               'source': 'oxfordstrat:heikin-ashi-1'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
