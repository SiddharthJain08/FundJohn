"""Wyckoff mean-reversion (oxfordstrat.com/trading-strategies/richard-wyckoff-mean-reversion-1/).
DOCUMENTED ADAPTATION of Oxford's full A/B/C/D bear-trap / bull-trap pattern. Oxford
defines a 4-point trading range (A,B,C,D) with a buy stop one tick above
Entry_Level = Price(B) + |Price(B)-Price(C)| * Entry_Index after a >= Pattern_Size-bar
correction. We collapse this to a swing-pivot reading (plan-sanctioned): after a down
leg, C = the most-recent confirmed swing LOW, B = the swing HIGH preceding it; LONG
when close reclaims Entry_Level = C + Entry_Index*(B-C) (Entry_Index 0.5 = midpoint).
Symmetric SHORT off a recent swing HIGH after an up leg (Oxford has both traps). This
is MEAN-REVERSION (reclaim toward the range), active in TRANSITIONING/HIGH_VOL.
Condition at the last bar (Oxford's [i-1]→open[i]; engine fills close[t+1]).
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, swing_pivots, atr

__all__ = ['OxfWyckoffMeanrev']


class OxfWyckoffMeanrev(OxfordBaseStrategy):
    id                = 'oxf_wyckoff_meanrev'
    name              = 'Oxford Wyckoff Mean Reversion (adaptation)'
    description       = 'Wyckoff trap mean-reversion on liquid ETFs — swing-pivot reclaim adaptation of the A/B/C/D pattern (oxfordstrat richard-wyckoff-mean-reversion-1). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 60
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'pattern_size': 10, 'pivot_k': 3, 'entry_index': 0.5}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        k = int(p['pivot_k'])
        pattern_size = int(p['pattern_size'])
        entry_index = float(p['entry_index'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < pattern_size + 2 * k + 2:
                continue
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            highs, lows = swing_pivots(bars, k)
            if not highs or not lows:
                continue
            close = float(bars['close'].iloc[-1])
            last_low_idx, last_low = lows[-1]
            last_high_idx, last_high = highs[-1]
            direction = None
            edge = 0.0
            # LONG (bear trap): a recent down leg = swing high B BEFORE swing low C.
            # Require the correction (bars since C) >= pattern_size.
            prior_highs = [(j, pr) for (j, pr) in highs if j < last_low_idx]
            if prior_highs and (len(bars) - 1 - last_low_idx) >= pattern_size:
                b_price = prior_highs[-1][1]
                c_price = last_low
                if b_price > c_price:
                    entry_level = c_price + entry_index * (b_price - c_price)
                    if close >= entry_level:
                        direction, edge = 'LONG', (close - c_price) / a
            # SHORT (bull trap): a recent up leg = swing low B BEFORE swing high C.
            if direction is None:
                prior_lows = [(j, pr) for (j, pr) in lows if j < last_high_idx]
                if prior_lows and (len(bars) - 1 - last_high_idx) >= pattern_size:
                    b_price = prior_lows[-1][1]
                    c_price = last_high
                    if c_price > b_price:
                        entry_level = c_price - entry_index * (c_price - b_price)
                        if close <= entry_level:
                            direction, edge = 'SHORT', (c_price - close) / a
            if direction is None:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'MED' if edge >= 1.0 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'pattern_size': pattern_size, 'entry_index': entry_index,
                               'reclaim_atr': round(float(edge), 3), 'regime': regime_state,
                               'source': 'oxfordstrat:richard-wyckoff-mean-reversion-1',
                               'note': 'swing-pivot reclaim adaptation of A/B/C/D trap'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
