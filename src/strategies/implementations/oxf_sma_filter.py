"""Simple moving-average trend filter (oxfordstrat.com/trading-strategies/simple-moving-average/).
Faithful daily-bar rule: long when close > SMA(slow) AND SMA(fast) > SMA(slow);
short = mirror. Condition evaluated at close[t]; the engine fills at close[t+1].
House regime-scaled brackets. ETF-basket cross-section.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, sma

__all__ = ['OxfSmaFilter']


class OxfSmaFilter(OxfordBaseStrategy):
    id                = 'oxf_sma_filter'
    name              = 'Oxford SMA Trend Filter'
    description       = 'Dual simple-MA trend filter on liquid ETFs (oxfordstrat simple-moving-average). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 270
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'slow': 250, 'fast': 63}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        slow, fast = int(p['slow']), int(p['fast'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < slow + 1:
                continue
            cseries = bars['close']
            close = float(cseries.iloc[-1])
            ma_slow = sma(cseries, slow)
            ma_fast = sma(cseries, fast)
            if ma_slow != ma_slow or ma_fast != ma_fast or ma_slow <= 0:
                continue
            if close > ma_slow and ma_fast > ma_slow:
                direction, edge = 'LONG', (close - ma_slow) / ma_slow
            elif close < ma_slow and ma_fast < ma_slow:
                direction, edge = 'SHORT', (ma_slow - close) / ma_slow
            else:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if edge >= 0.10 else 'MED' if edge >= 0.03 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'slow': slow, 'fast': fast, 'edge': round(float(edge), 4),
                               'regime': regime_state, 'source': 'oxfordstrat:simple-moving-average'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
