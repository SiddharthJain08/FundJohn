"""Livermore reaction/pivotal-point system (oxfordstrat.com/trading-strategies/livermore-system-1/).
Faithful daily-bar rule: LONG when close penetrates the 2 most-recent (significant)
supply pivots [swing highs] by >= Noise; SHORT symmetrically below the 2 most-recent
demand pivots [swing lows]. Oxford: a supply pivot is a local high FOLLOWED by a
bearish swing of >= Swing_Filter ATR; Noise = smallest of the 2 most-recent swings *
Penetration_Filter. The page does NOT specify the local-extreme window, so we detect
local extremes with the general fractal swing_pivots (k=2, documented choice) and
keep only pivots whose subsequent swing >= Swing_Filter * ATR(20). Condition at the
last bar (Oxford's [i-1]→open[i]; engine fills close[t+1]). House ATR brackets.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, swing_pivots, atr

__all__ = ['OxfLivermore']


class OxfLivermore(OxfordBaseStrategy):
    id                = 'oxf_livermore'
    name              = 'Oxford Livermore Pivotal-Point System'
    description       = 'Livermore pivotal-point breakout on liquid ETFs (oxfordstrat livermore-system-1). Penetrates 2 most-recent swing pivots by >= noise. Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 60
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'pivot_k': 2, 'swing_filter': 4.0, 'penetration': 0.5}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        k = int(p['pivot_k'])
        swing_filter = float(p['swing_filter'])
        penetration = float(p['penetration'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < 40:
                continue
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            min_swing = swing_filter * a
            highs, lows = swing_pivots(bars, k)
            close = float(bars['close'].iloc[-1])
            # Keep only SIGNIFICANT supply pivots: a swing high followed by a
            # decline of >= min_swing (vs the latest close, a causal proxy for the
            # subsequent bearish swing). Take the 2 most recent.
            sig_highs = [(j, pr) for (j, pr) in highs if (pr - close) >= min_swing or (pr - bars['low'].iloc[j:].min()) >= min_swing]
            sig_lows = [(j, pr) for (j, pr) in lows if (close - pr) >= min_swing or (bars['high'].iloc[j:].max() - pr) >= min_swing]
            direction = None
            edge = 0.0
            if len(sig_highs) >= 2:
                p1, p2 = sig_highs[-1][1], sig_highs[-2][1]
                # Noise = smallest of the 2 most-recent swing magnitudes * penetration.
                sw1 = abs(sig_highs[-1][1] - close)
                sw2 = abs(sig_highs[-2][1] - close)
                noise = min(sw1, sw2) * penetration
                trigger = max(p1, p2) + noise
                if close > trigger:
                    direction, edge = 'LONG', (close - trigger) / a
            if direction is None and len(sig_lows) >= 2:
                q1, q2 = sig_lows[-1][1], sig_lows[-2][1]
                sw1 = abs(close - sig_lows[-1][1])
                sw2 = abs(close - sig_lows[-2][1])
                noise = min(sw1, sw2) * penetration
                trigger = min(q1, q2) - noise
                if close < trigger:
                    direction, edge = 'SHORT', (trigger - close) / a
            if direction is None:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if edge >= 1.0 else 'MED' if edge >= 0.3 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'swing_filter': swing_filter, 'penetration': penetration,
                               'pivot_edge_atr': round(float(edge), 3), 'regime': regime_state,
                               'source': 'oxfordstrat:livermore-system-1'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
