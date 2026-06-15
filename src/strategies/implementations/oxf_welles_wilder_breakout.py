"""Welles Wilder volatility breakout (oxfordstrat.com/trading-strategies/welles-wilder-1/).

CONFIRMED-BREAKOUT DAILY-BAR ADAPTATION (+ SIC adaptation): Oxford's rule is
"a buy on the close when the close is above the ARC distance point from the SIC",
where ARC = ATR(ATR_Length) * Constant and SIC ("Significant Close") is the
extreme favorable close reached WHILE IN A TRADE. SIC is a path/state-dependent
quantity that cannot be computed statelessly at signal-generation time, so any
entry reading is an ADAPTATION. We anchor the breakout reference to the close
`lookback` bars ago and require the move over that window to exceed ARC — i.e.
Close[i] − Close[i − lookback] > ARC for a long (a volatility-scaled momentum
breakout, matching the page's "volatility breakout" framing); short = mirror.
The engine fills at close[t+1]. Documented divergence: stateless SIC proxy.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, atr

__all__ = ['OxfWellesWilderBreakout']


class OxfWellesWilderBreakout(OxfordBaseStrategy):
    id                = 'oxf_welles_wilder_breakout'
    name              = 'Oxford Welles Wilder Volatility Breakout (adaptation)'
    description       = ('Welles Wilder volatility breakout on liquid ETFs — confirmed-breakout '
                         'daily-bar adaptation (oxfordstrat welles-wilder-1). Close move over the '
                         'lookback exceeds ARC = ATR*const (stateless SIC proxy); close[t+1] fill.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 40
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'lb': 20, 'const': 3.0, 'atr_length': 20}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        lb = int(p['lb'])
        const = float(p['const'])
        atr_len = int(p['atr_length'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < max(lb, atr_len) + 2:
                continue
            a = atr(bars, atr_len)
            if a is None or a != a or a <= 0:
                continue
            arc = a * const
            close_now = float(bars['close'].iloc[-1])
            ref_close = float(bars['close'].iloc[-1 - lb])  # SIC proxy: close lb bars ago
            move = close_now - ref_close
            direction, edge = None, 0.0
            if move > arc:
                direction, edge = 'LONG', move / arc
            elif move < -arc:
                direction, edge = 'SHORT', (-move) / arc
            if direction is None:
                continue
            ranked.append((edge, t, direction, close_now, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close_now, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close_now, regime_state=regime_state)
            conf = 'HIGH' if edge >= 1.5 else 'MED' if edge >= 1.1 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close_now,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'lb': lb, 'const': const, 'arc_mult': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:welles-wilder-1',
                               'note': 'confirmed-breakout daily-bar adaptation (stateless SIC proxy)'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
