"""
Index Trend-Filtered Opening-Range Breakout.

Source: https://yuratrading.com/blogs/index-orb
"The 9:30 Opening-Range Breakout Is Just Long the Market in Disguise"

The source backtests the popular 9:30-candle opening-range-breakout (ORB)
rule across SPY/QQQ/IWM with ~3 years of 1-minute data and finds no
independent directional edge: breakout shorts and counter-trend longs carry
no edge at all, and the only consistently profitable subset is a long
breakout taken while the broader index trend is already up. The authors'
conclusion is that the real exploitable signal is a long-only trend filter,
not the breakout trigger — ORB is "just long the market in disguise."

Translation to our 30-minute RTH bar grid (we do not carry 1-minute bars):
the opening range is approximated by the 09:30 bar's high/low (the finest
granularity available). If the 10:00 bar closes above the opening-range high
AND the prevailing daily trend is up (last daily close > SMA_50), go LONG,
entering at the 10:30 bar open and flattening at the session close via the
intraday session simulator. Breakout shorts and counter-trend longs are
discarded per the paper's falsification — no SHORT signals are ever emitted.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal
from strategies.cohort_base import tag_rth_30m
from strategies.intraday import session_close_params

__all__ = ['IndexTrendFilteredORB']

INSTRUMENT_CLASS = 'etp'

# Three major US index ETPs — the ETP analog of the source paper's
# SPY/QQQ/IWM basket.
BASKET = ('SPY', 'QQQ', 'IWM')

OR_BAR = '09:30'      # opening-range proxy: the first RTH 30-min bar
DECISION_BAR = '10:00'  # breakout confirmed on this bar's close
SMA_WINDOW = 50


class IndexTrendFilteredORB(BaseStrategy):
    """Long-only ORB: trade the 9:30 opening-range breakout only when the daily trend is already up."""

    id                = 'S_index_trend_filtered_orb'
    name              = 'IndexTrendFilteredORB'
    description       = (
        "9:30 opening-range breakout on SPY/QQQ/IWM, taken long-only and "
        "only when price is above its 50-day SMA; the source paper found no "
        "edge in breakout shorts or counter-trend longs."
    )
    tier              = 2
    min_lookback      = SMA_WINDOW + 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    MAX_SIGNALS = 3

    def default_parameters(self) -> dict:
        return {
            'pos_size_frac': 0.04,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        prices_30m = aux_data.get('prices_30m') if aux_data else None
        if prices_30m is None or prices_30m.empty:
            print('[debug] signals=0 (no prices_30m)', file=sys.stderr)
            return []

        tagged = tag_rth_30m(prices_30m)
        if tagged.empty:
            print('[debug] signals=0 (no RTH bars)', file=sys.stderr)
            return []

        today      = tagged['session'].max()
        today_bars = tagged[tagged['session'] == today]

        scale    = self.position_scale(regime_state)
        pos_size = round(self.parameters.get('pos_size_frac', 0.04) * scale, 4)

        signals: List[Signal] = []

        for ticker in BASKET:
            entry_series = prices[ticker].dropna() if ticker in prices.columns else pd.Series(dtype=float)
            if len(entry_series) < SMA_WINDOW + 1:
                continue

            trend_up = float(entry_series.iloc[-1]) > float(entry_series.tail(SMA_WINDOW).mean())
            if not trend_up:
                continue

            t_bars = today_bars[today_bars['ticker'] == ticker].dropna(subset=['high', 'low', 'close'])
            or_bar = t_bars[t_bars['time_et'] == OR_BAR]
            decision_bar = t_bars[t_bars['time_et'] == DECISION_BAR]
            if or_bar.empty or decision_bar.empty:
                continue

            or_high = float(or_bar['high'].iloc[0])
            decision_close = float(decision_bar['close'].iloc[0])

            breakout = decision_close > or_high
            if not breakout:
                continue

            stops = self.compute_stops_and_targets(
                entry_series,
                'LONG',
                decision_close,
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = decision_close,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = 'MED',
                signal_params     = session_close_params(
                    '10:30',
                    opening_range_high=round(or_high, 4),
                    trend_filter='sma_50_up',
                    regime=regime_state,
                ),
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
