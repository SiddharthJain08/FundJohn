"""
5-Minute Opening Range Breakout -- classic ORB, debunking audit.

Source: https://nulliusresearch.substack.com/p/5-minute-opening-range-breakout-7217
"5-Minute Opening Range Breakout: 72.17% Claimed, 65.9% Measured" (Nullius Research, 2026)

The source re-tests the retail-popular 5-minute opening-range-breakout rule
(long on a break above the first 5 minutes' high, short on a break below the
first 5 minutes' low, exit at the close) against a random-entry control
group. The measured win rate (65.9% on 1,250 S&P 500 trades) sits below
BOTH the 66.7% cost-adjusted breakeven AND the 67% random-entry control --
the authors' own conclusion is a debunking, not an alpha claim ("no edge
detected").

Translation to our 30-minute RTH bar grid (no 5-minute bars in the ledger):
the opening range is approximated by the 09:30 bar's high/low -- the finest
granularity available -- the S&P-500-wide analog of the paper's
methodology. If the 10:00 bar closes above the opening-range high -> LONG;
below the opening-range low -> SHORT. Entry at the 10:30 bar open via the
intraday session simulator, flat at the close (the paper's own EOD exit
convention). No trend filter, no discretionary overlay: this is a
faithful implementation of the classic (already-debunked) rule, so the
regime-partitioned auto-backtest is expected to corroborate the source's
"no edge" finding rather than surface a new one.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal
from strategies.cohort_base import tag_rth_30m
from strategies.intraday import session_close_params
from src.strategies.universe_default import sp500 as universe_filter

__all__ = ['FiveMinORBSP500']

INSTRUMENT_CLASS = 'equity'

OR_BAR = '09:30'        # opening-range proxy: first RTH 30-min bar
DECISION_BAR = '10:00'  # breakout confirmed on this bar's close
# Entry fill is the '10:30' bar open (first bar after DECISION_BAR), passed
# as a literal to session_close_params -- the intraday lint requires a
# string literal there, not a module constant.


class FiveMinORBSP500(BaseStrategy):
    """Classic opening-range breakout on S&P 500 names: long above / short
    below the 09:30 bar's range, flat at the close. The source audit
    measured no edge (65.9% win rate, below both breakeven and a
    random-entry control)."""

    id                = 'S_five_min_orb_sp500'
    name              = 'FiveMinORBSP500'
    description       = (
        "Classic opening-range breakout: long on a break above / short on a "
        "break below the 09:30 bar's range, flat at the close. Nullius "
        "Research's own audit measured 65.9% win rate on 1,250 S&P 500 "
        "trades -- below both the cost-adjusted breakeven and the "
        "random-entry control, i.e. no edge detected."
    )
    tier              = 3
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    MAX_SIGNALS = 40

    def default_parameters(self) -> dict:
        return {
            'pos_size_frac': 0.02,
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
        pos_size = round(self.parameters.get('pos_size_frac', 0.02) * scale, 4)

        signals: List[Signal] = []

        for ticker in universe:
            if ticker not in prices.columns:
                continue
            entry_series = prices[ticker].dropna()
            if entry_series.empty:
                continue

            t_bars = today_bars[today_bars['ticker'] == ticker].dropna(subset=['high', 'low', 'close'])
            or_bar = t_bars[t_bars['time_et'] == OR_BAR]
            decision_bar = t_bars[t_bars['time_et'] == DECISION_BAR]
            if or_bar.empty or decision_bar.empty:
                continue

            or_high = float(or_bar['high'].iloc[0])
            or_low  = float(or_bar['low'].iloc[0])
            decision_close = float(decision_bar['close'].iloc[0])

            if decision_close > or_high:
                direction = 'LONG'
            elif decision_close < or_low:
                direction = 'SHORT'
            else:
                continue

            stops = self.compute_stops_and_targets(
                entry_series,
                direction,
                decision_close,
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = decision_close,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = 'LOW',
                signal_params     = session_close_params(
                    '10:30',
                    opening_range_high=round(or_high, 4),
                    opening_range_low=round(or_low, 4),
                    regime=regime_state,
                ),
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
