"""Keltner Channels breakout (oxfordstrat.com/trading-strategies/keltner-channels-1/).
Faithful daily-bar rule: LONG when Close > Buy_Line; SHORT when Close < Sell_Line,
where Buy/Sell_Line = SMA(TypicalPrice, lb) +/- Range_Multiple * SMA(High-Low, lb).
DIVERGENCE: Oxford's keltner-channels-1 bands use AVERAGE RANGE (SMA of High-Low),
NOT ATR — implemented per the page. Condition evaluated at the last bar (Oxford's
[i-1]→open[i]; the engine fills at close[t+1]). ETF-basket; house ATR brackets.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, keltner

__all__ = ['OxfKeltner']


class OxfKeltner(OxfordBaseStrategy):
    id                = 'oxf_keltner'
    name              = 'Oxford Keltner Channel Breakout'
    description       = 'Keltner-channel breakout on liquid ETFs (oxfordstrat keltner-channels-1; average-range bands, not ATR). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'look_back': 10, 'range_multiple': 1.0}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        lb = int(p['look_back'])
        mult = float(p['range_multiple'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < lb + 2:
                continue
            mid, buy_line, sell_line = keltner(bars, lb, mult)
            if mid != mid or buy_line != buy_line:
                continue
            close = float(bars['close'].iloc[-1])
            band = buy_line - mid
            if band <= 0:
                continue
            if close > buy_line:
                direction, edge = 'LONG', (close - buy_line) / band
            elif close < sell_line:
                direction, edge = 'SHORT', (sell_line - close) / band
            else:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if edge >= 1.0 else 'MED' if edge >= 0.25 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'look_back': lb, 'range_multiple': mult,
                               'band_edge': round(float(edge), 3), 'regime': regime_state,
                               'source': 'oxfordstrat:keltner-channels-1'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
