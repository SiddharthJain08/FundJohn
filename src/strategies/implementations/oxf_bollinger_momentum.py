"""Bollinger Bands momentum breakout (oxfordstrat.com/trading-strategies/bollinger-bands/).
Faithful daily-bar rule: LONG when close pierces the upper band (SMA + k*sigma);
SHORT when close pierces the lower band (SMA - k*sigma) — momentum/breakout reading
(not mean-reversion). Condition evaluated at the last bar (Oxford's [i-1]→open[i];
the engine fills at close[t+1]). ETF-basket cross-section; house ATR brackets.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, bollinger

__all__ = ['OxfBollingerMomentum']


class OxfBollingerMomentum(OxfordBaseStrategy):
    id                = 'oxf_bollinger_momentum'
    name              = 'Oxford Bollinger Band Momentum'
    description       = 'Bollinger-band momentum breakout on liquid ETFs (oxfordstrat bollinger-bands). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 100
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'ma_len': 80, 'k': 2.0}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        n = int(p['ma_len'])
        k = float(p['k'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < n + 2:
                continue
            mid, up, lo = bollinger(bars['close'], n, k)
            if mid != mid or up != up:
                continue
            close = float(bars['close'].iloc[-1])
            width = up - mid
            if width <= 0:
                continue
            if close > up:
                direction, edge = 'LONG', (close - up) / width
            elif close < lo:
                direction, edge = 'SHORT', (lo - close) / width
            else:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if edge >= 0.5 else 'MED' if edge >= 0.1 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'ma_len': n, 'k': k, 'band_edge': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:bollinger-bands'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
