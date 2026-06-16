"""Zero-Lag Moving Average (oxfordstrat.com/trading-strategies/zero-lag-moving-average/).
Faithful daily-bar rule: long when ZLMA > EMA AND (100*err/ATR) > threshold;
short = mirror (ZLMA < EMA AND 100*|err|/ATR > threshold). err = Close - ZLMA.

DEVIATION (surfaced for operator): Oxford optimizes the ZLMA `gain` per bar via an
error-minimizing loop (non-causal, path-dependent, costly). We use a FIXED gain
(default 5 = Oxford Gain_Limit). The ZLMA recursion itself is the faithful Oxford
form; only the per-bar gain meta-optimization is omitted.
Condition evaluated at close[t]; engine fills at close[t+1]. House brackets.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, zlma, ema, atr

__all__ = ['OxfZeroLagMa']


class OxfZeroLagMa(OxfordBaseStrategy):
    id                = 'oxf_zero_lag_ma'
    name              = 'Oxford Zero-Lag Moving Average'
    description       = 'Zero-Lag MA vs EMA with normalized-error filter on liquid ETFs (oxfordstrat zero-lag-moving-average; fixed-gain adaptation of the gain-optimization loop). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 230
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'lookback': 200, 'threshold': 50, 'gain': 5.0, 'atr_len': 20}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        lb = int(p['lookback']); thr = float(p['threshold'])
        gain = float(p['gain']); alen = int(p['atr_len'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            cseries = bars['close']
            if len(cseries) < lb + 2:
                continue
            z, err = zlma(cseries, lb, gain=gain)
            e = ema(cseries, lb)
            a = atr(bars, alen)
            close = float(cseries.iloc[-1])
            if z != z or e != e or a is None or a != a or a <= 0:
                continue
            norm_err = 100.0 * abs(err) / a
            if norm_err <= thr:
                continue
            if z > e and err > 0:
                direction, edge = 'LONG', norm_err
            elif z < e and err < 0:
                direction, edge = 'SHORT', norm_err
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
                signal_params={'lookback': lb, 'threshold': thr, 'norm_err': round(float(edge), 2),
                               'regime': regime_state, 'source': 'oxfordstrat:zero-lag-moving-average',
                               'note': 'fixed-gain adaptation (gain-optimization loop omitted)'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
