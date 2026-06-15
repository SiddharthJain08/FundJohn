"""RSI(2) mean-reversion (oxfordstrat.com/trading-strategies/relative-strength-index-1/).
Faithful daily-bar MEAN-REVERSION rule (Connors-style RSI2): LONG ONLY when the
asset is in a long-term uptrend (close > SMA(trend)) AND the short RSI is deeply
oversold (RSI(2) < threshold). Oxford's RSI2 system buys dips inside an uptrend;
it is long-only, so no short mirror. Active in TRANSITIONING / HIGH_VOL (the
pullback regimes), per the plan. Condition at close[t]; engine fills at close[t+1].
House brackets (the engine's stop/target/max-hold provide the exit; Oxford's
"close > SMA(5)" exit is subsumed by the house bracket every candidate uses).
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, sma, rsi_wilder

__all__ = ['OxfRsi2Meanrev']


class OxfRsi2Meanrev(OxfordBaseStrategy):
    id                = 'oxf_rsi2_meanrev'
    name              = 'Oxford RSI(2) Mean-Reversion'
    description       = 'RSI(2) oversold dip-buy inside an SMA(200) uptrend on liquid ETFs, long-only mean-reversion (oxfordstrat relative-strength-index-1). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 210
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'rsi_len': 2, 'trend': 200, 'thr': 5}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        rlen = int(p['rsi_len']); trend = int(p['trend']); thr = float(p['thr'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            cseries = bars['close']
            if len(cseries) < trend + 1:
                continue
            close = float(cseries.iloc[-1])
            ma_trend = sma(cseries, trend)
            r = rsi_wilder(cseries, rlen)
            if ma_trend != ma_trend or r != r or ma_trend <= 0:
                continue
            # Long-only dip-buy: uptrend filter + deep short-RSI oversold.
            if close > ma_trend and r < thr:
                edge = (thr - r)  # the more oversold, the stronger
                ranked.append((edge, t, 'LONG', close, bars))
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
                signal_params={'rsi_len': rlen, 'trend': trend, 'rsi_oversold_by': round(float(edge), 3),
                               'regime': regime_state, 'source': 'oxfordstrat:relative-strength-index-1',
                               'note': 'long-only mean-reversion'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
