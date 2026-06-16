"""Donchian channel breakout (oxfordstrat.com/trading-strategies/donchian-channel-2/).
Faithful daily-bar rule: enter when the close pierces the prior N-day Donchian
channel. Condition is evaluated at close[t]; the engine fills at close[t+1].
ATR(20)-multiple brackets (Oxford ATR_Stop default). ETF-basket cross-section.
"""
from __future__ import annotations
import sys
from typing import List
import pandas as pd
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, donchian_prev, atr

__all__ = ['OxfDonchianBreakout']


class OxfDonchianBreakout(OxfordBaseStrategy):
    id                = 'oxf_donchian_breakout'
    name              = 'Oxford Donchian Channel Breakout'
    description       = 'Donchian N-day channel breakout on liquid ETFs (oxfordstrat donchian-channel-2). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 60
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'channel_length': 40}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        n = int(p['channel_length'])
        # IGNORE the `universe` arg (the backtest may pass an sp500-scoped list
        # that excludes ETFs). Iterate the self-loaded basket OHLC directly —
        # the proven S_commodity_etp_momentum pattern. Fills only need the
        # ticker in the full panel's bars_by_ticker, which the basket is.
        ohlc = self.basket_ohlc(prices)
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < n + 2:
                continue
            up, lo = donchian_prev(bars, n)
            close = float(bars['close'].iloc[-1])
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            if close > up:
                direction, dist = 'LONG', (close - up) / a
            elif close < lo:
                direction, dist = 'SHORT', (lo - close) / a
            else:
                continue
            ranked.append((dist, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        signals: List[Signal] = []
        keep = ranked[:self.MAX_SIGNALS]
        for dist, t, direction, close, bars in keep:
            # House brackets (same as every candidate): regime-scaled ATR stop + 5/10/20% targets.
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if dist >= 1.0 else 'MED' if dist >= 0.3 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0/max(len(keep),1))*0.18*scale, 4),
                confidence=conf,
                signal_params={'channel_length': n, 'breakout_atr': round(float(dist),3),
                               'regime': regime_state, 'source': 'oxfordstrat:donchian-channel-2'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
