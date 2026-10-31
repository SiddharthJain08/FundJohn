"""
Fade Quarterly Theory Q2 — mechanical fade of a falsified session-timing claim.

Source: https://yuratrading.com/blogs/quarterly-theory
"Quarterly Theory Says the Day's Extreme Forms in Q2. It's the Least Likely
Quarter."

The source backtests the popular "Quarterly Theory" claim that a session's
high/low extreme forms during its second quarter (Q2) and finds the claim is
empirically false: Q2 contained the day's extreme only 16.6% of the time vs
a 25% base rate across 2,162 trades (FX majors, US indices, gold, 2019-2021,
-0.06R/trade with costs). The authors report no independent LONG/SHORT
directional edge — the only residual edge is mechanical: a new extreme SET
during Q2 is statistically unlikely to be the day's real extreme, so it is
biased to be given back later in the session.

Translation to a tradeable rule on US ETPs: split the 13 RTH 30-minute bars
(09:30-16:00 ET) into quarters. If price sets a new intraday high during Q2
that was not already set in Q1, FADE it (SHORT) — the "fake" extreme is
statistically likely to be taken out later. Symmetric for a new intraday low
(LONG). Enter at the fixed 13:00 ET bar (first bar after the Q2 read window),
flat at the session close via the intraday session simulator.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal
from strategies.cohort_base import tag_rth_30m
from strategies.intraday import session_close_params

__all__ = ['FadeQuarterlyTheoryQ2']

INSTRUMENT_CLASS = 'etp'

# Liquid ETPs with prices_30m coverage — the ETP analog of the source
# paper's FX-majors / US-indices / gold basket.
BASKET = ('SPY', 'QQQ', 'IWM', 'DIA', 'GLD', 'SLV', 'USO')

# RTH session (09:30-16:00 ET) split into quarters over the 13 30-min bars.
Q1_BARS = ('09:30', '10:00', '10:30')
Q2_BARS = ('11:00', '11:30', '12:00', '12:30')
# Entry fires at 13:00 ET — the first RTH bar strictly after the Q2 window.


class FadeQuarterlyTheoryQ2(BaseStrategy):
    """Fade a new day-high/day-low set during Q2 — the published Q2-extreme rule tests below chance."""

    id                = 'S_fade_quarterly_theory_q2'
    name              = 'FadeQuarterlyTheoryQ2'
    description       = (
        "Fade a new day-high/day-low set during the Q2 session window; the "
        "published 'extreme forms in Q2' rule tests below the 25% chance rate "
        "(16.6% hit rate, 2,162 trades, 2019-2021)."
    )
    tier              = 2
    min_lookback      = 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    MAX_SIGNALS = 4

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
            t_bars = today_bars[today_bars['ticker'] == ticker].dropna(subset=['high', 'low', 'close'])
            q1 = t_bars[t_bars['time_et'].isin(Q1_BARS)]
            q2 = t_bars[t_bars['time_et'].isin(Q2_BARS)]
            if q1.empty or q2.empty:
                continue

            q1_high, q1_low = float(q1['high'].max()), float(q1['low'].min())
            q2_high, q2_low = float(q2['high'].max()), float(q2['low'].min())

            new_high_in_q2 = q2_high > q1_high
            new_low_in_q2  = q2_low < q1_low

            if new_high_in_q2 and not new_low_in_q2:
                direction, extreme_px = 'SHORT', q2_high
            elif new_low_in_q2 and not new_high_in_q2:
                direction, extreme_px = 'LONG', q2_low
            else:
                # Both or neither quarter set a new extreme — no fade edge.
                continue

            entry_series = prices[ticker].dropna() if ticker in prices.columns else pd.Series(dtype=float)
            if len(entry_series) < 14:
                continue

            entry_price = float(q2.sort_values('time_et')['close'].iloc[-1])

            stops = self.compute_stops_and_targets(
                entry_series,
                direction,
                entry_price,
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = entry_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = 'MED',
                signal_params     = session_close_params(
                    '13:00',
                    q2_extreme=round(extreme_px, 4),
                    faded_side='high' if direction == 'SHORT' else 'low',
                    regime=regime_state,
                ),
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
