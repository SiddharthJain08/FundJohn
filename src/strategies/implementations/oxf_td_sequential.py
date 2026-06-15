"""TD Sequential setup (oxfordstrat.com/trading-strategies/td-sequential-1/).
Faithful daily-bar rule (Method 2): a BUY setup = >= setup (9) consecutive closes
each BELOW the close 4 bars earlier (bottom exhaustion). The LONG fires on the
SUBSEQUENT up-flip — the first bar after a completed buy setup where Close > Close[i-4].
Symmetric SHORT: a SELL setup (>= setup consecutive closes ABOVE close[i-4], top
exhaustion) then a down-flip Close < Close[i-4]. Condition at the last bar (Oxford's
[i-1]→open[i]; engine fills close[t+1]). ETF-basket; house ATR brackets.
"""
from __future__ import annotations
import sys
from typing import List
import numpy as np
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, atr

__all__ = ['OxfTdSequential']


def _signed_run(c: np.ndarray, end: int) -> int:
    """Signed TD setup-run length ending at bar `end` (-down/+up; 0 tie/short)."""
    if end < 4:
        return 0
    sign = 0
    count = 0
    for i in range(4, end + 1):
        if c[i] < c[i - 4]:
            cur = -1
        elif c[i] > c[i - 4]:
            cur = 1
        else:
            cur = 0
        if cur == 0:
            sign, count = 0, 0
        elif cur == sign:
            count += 1
        else:
            sign, count = cur, 1
    return sign * count


class OxfTdSequential(OxfordBaseStrategy):
    id                = 'oxf_td_sequential'
    name              = 'Oxford TD Sequential Setup'
    description       = 'TD Sequential setup(9) exhaustion + reversal flip on liquid ETFs (oxfordstrat td-sequential-1). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'setup': 9}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        setup = int(p['setup'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < setup + 6:
                continue
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            c = bars['close'].to_numpy(dtype=float)
            last = len(c) - 1
            if not (np.isfinite(c[last]) and np.isfinite(c[last - 4])):
                continue
            close = float(c[last])
            prev_run = _signed_run(c, last - 1)  # completed run BEFORE the latest bar
            direction = None
            edge = 0.0
            # LONG: a >= setup BUY setup completed (prev_run <= -setup), then the
            # latest bar flips up (Close > Close[i-4]) — bottom-exhaustion reversal.
            if prev_run <= -setup and c[last] > c[last - 4]:
                direction, edge = 'LONG', abs(prev_run)
            # SHORT: a >= setup SELL setup completed (prev_run >= setup), then the
            # latest bar flips down — top-exhaustion reversal.
            elif prev_run >= setup and c[last] < c[last - 4]:
                direction, edge = 'SHORT', abs(prev_run)
            if direction is None:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if edge >= setup + 4 else 'MED'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0 / max(len(keep), 1)) * 0.18 * scale, 4),
                confidence=conf,
                signal_params={'setup': setup, 'completed_run': int(edge),
                               'regime': regime_state, 'source': 'oxfordstrat:td-sequential-1'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
