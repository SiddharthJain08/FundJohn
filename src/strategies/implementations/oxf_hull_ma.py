"""Hull Moving Average trend filter (oxfordstrat.com/trading-strategies/hull-moving-average/).
Faithful daily-bar rule (per plan/task): long when close > HMA(slow) AND
HMA(fast) > HMA(slow); short = mirror. Indicator is the exact Oxford nested-WMA
HMA (M=round(n/2), K=round(√n)). NOTE: Oxford's own write-up enters on BOTH HMAs
turning up (slope) — the plan deliberately uses the close/HMA-cross form to keep
this parallel to oxf_sma_filter; documented divergence, indicator unchanged.
Condition evaluated at close[t]; engine fills at close[t+1]. House brackets.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, hma

__all__ = ['OxfHullMa']


class OxfHullMa(OxfordBaseStrategy):
    id                = 'oxf_hull_ma'
    name              = 'Oxford Hull Moving Average'
    description       = 'Dual Hull-MA trend filter on liquid ETFs (oxfordstrat hull-moving-average). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 450
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'slow': 400, 'fast': 100}

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
            cseries = bars['close']
            if len(cseries) < slow + int(slow ** 0.5) + 2:
                continue
            close = float(cseries.iloc[-1])
            h_slow = hma(cseries, slow)
            h_fast = hma(cseries, fast)
            if h_slow != h_slow or h_fast != h_fast or h_slow <= 0:
                continue
            if close > h_slow and h_fast > h_slow:
                direction, edge = 'LONG', (close - h_slow) / h_slow
            elif close < h_slow and h_fast < h_slow:
                direction, edge = 'SHORT', (h_slow - close) / h_slow
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
                               'regime': regime_state, 'source': 'oxfordstrat:hull-moving-average'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
