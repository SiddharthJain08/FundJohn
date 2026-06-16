"""Vortex Indicator crossover (oxfordstrat.com/trading-strategies/vortex-indicator-1/).
Faithful daily-bar rule: long on a +VI/-VI bullish CROSSOVER — +VI[t] > -VI[t] AND
+VI[t-1] <= -VI[t-1]; short on the bearish crossover. The crossover (not a level)
is the Oxford trigger, so this rule fires sparsely per bar — crossovers accumulate
over the backtest history. Condition at close[t]; engine fills at close[t+1].
House brackets. ETF-basket cross-section.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, vortex

__all__ = ['OxfVortex']


class OxfVortex(OxfordBaseStrategy):
    id                = 'oxf_vortex'
    name              = 'Oxford Vortex Indicator'
    description       = '+VI/-VI crossover trend trigger on liquid ETFs (oxfordstrat vortex-indicator-1). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 120
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'lb': 110}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        lb = int(p['lb'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < lb + 2:
                continue
            pvi, nvi = vortex(bars, lb)
            pvi_p, nvi_p = vortex(bars.iloc[:-1], lb)  # prior bar
            if any(v != v for v in (pvi, nvi, pvi_p, nvi_p)):
                continue
            close = float(bars['close'].iloc[-1])
            # Strict crossover: +VI now above -VI and was at/below it on the prior bar.
            if pvi > nvi and pvi_p <= nvi_p:
                direction, edge = 'LONG', (pvi - nvi)
            elif pvi < nvi and pvi_p >= nvi_p:
                direction, edge = 'SHORT', (nvi - pvi)
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
                signal_params={'lb': lb, 'vi_gap': round(float(edge), 4),
                               'regime': regime_state, 'source': 'oxfordstrat:vortex-indicator-1'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
