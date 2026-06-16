"""Kaufman Adaptive Moving Average (oxfordstrat.com/trading-strategies/adaptive-moving-average-1/).
Faithful daily-bar rule:
  LONG  when AMA[i] > AMA[i-1]  AND  (AMA[i] - MinAMA over n) > Filter
  SHORT when AMA[i] < AMA[i-1]  AND  (MaxAMA over n - AMA[i]) > Filter
where Filter = Filter_Index * StdDev(ΔAMA, n)  (Oxford's exact filter form — the
plan's loose "filter·ATR" paraphrase is mapped to this faithful definition).
Condition evaluated at close[t]; engine fills at close[t+1]. House brackets.
"""
from __future__ import annotations
import sys
from typing import List
import numpy as np
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, kaufman_ama_series

__all__ = ['OxfAdaptiveMa']


class OxfAdaptiveMa(OxfordBaseStrategy):
    id                = 'oxf_adaptive_ma'
    name              = 'Oxford Kaufman Adaptive MA'
    description       = 'Kaufman AMA trend with std-dev filter on liquid ETFs (oxfordstrat adaptive-moving-average-1). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 80
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        # er_len=20 (efficiency-ratio window), fast/slow EMA lengths 2/30,
        # filter_index=0.01 scaling the std-dev of AMA increments, filter window n=20.
        return {'er_len': 20, 'fast': 2, 'slow': 30, 'filter_index': 0.01, 'filter_n': 20}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        er_len = int(p['er_len']); fast = int(p['fast']); slow = int(p['slow'])
        fidx = float(p['filter_index']); fn = int(p['filter_n'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            cseries = bars['close']
            if len(cseries) < er_len + fn + 2:
                continue
            ama = kaufman_ama_series(cseries, er_len, fast, slow).dropna()
            if len(ama) < fn + 2:
                continue
            a_now = float(ama.iloc[-1]); a_prev = float(ama.iloc[-2])
            damma = ama.diff().dropna()
            if len(damma) < fn:
                continue
            filt = fidx * float(damma.iloc[-fn:].std(ddof=0))
            window = ama.iloc[-fn:]
            min_ama = float(window.min()); max_ama = float(window.max())
            close = float(cseries.iloc[-1])
            if a_now > a_prev and (a_now - min_ama) > filt:
                direction, edge = 'LONG', (a_now - min_ama)
            elif a_now < a_prev and (max_ama - a_now) > filt:
                direction, edge = 'SHORT', (max_ama - a_now)
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
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence='MED',
                signal_params={'er_len': er_len, 'filter_index': fidx, 'ama_swing': round(float(edge), 4),
                               'regime': regime_state, 'source': 'oxfordstrat:adaptive-moving-average-1'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
