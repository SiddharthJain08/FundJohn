"""
Intramonth Momentum Cycle (Basilico 2026 / Alpha Architect).

Source: https://alphaarchitect.com/momentum-cycle/

Institutional 'dash-for-cash' selling of momentum losers in the 6 trading
days before month-end concentrates the bulk of momentum profits into a
predictable calendar window, enabling far more efficient momentum capture
than continuous holding.

Signal: cross-sectional 12-1M momentum rank; long top quintile / short
bottom quintile ONLY during the 6 active trading days before month-end.
FLAT otherwise.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import sp500 as universe_filter

__all__ = ['IntramontMomentumCycle']

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = 'S_intramonth_momentum_cycle'

# Calendar constants
LOOKBACK_LONG = 252   # 12-month return lookback
LOOKBACK_SKIP = 21    # skip most-recent 1 month (reversal bias)
ACTIVE_DAYS   = 5     # 0-indexed: days -5 through 0 before last trading day of month
QUINTILE      = 0.20  # top/bottom 20%
BASE_SIZE     = 0.015 # per-position base size (fraction of portfolio)


def _trading_days_until_month_end(date: pd.Timestamp, all_dates: pd.DatetimeIndex) -> int:
    """Return how many trading days remain until (and including) the last trading
    day of the current calendar month.  Returns 0 on the last trading day."""
    month_end_dates = all_dates[(all_dates.month == date.month) & (all_dates.year == date.year)]
    if len(month_end_dates) == 0:
        return 999
    last_day = month_end_dates[-1]
    future = all_dates[(all_dates >= date) & (all_dates <= last_day)]
    # subtract 1 so the last trading day itself returns 0
    return max(len(future) - 1, 0)


class IntramontMomentumCycle(BaseStrategy):
    """Long top-quintile / short bottom-quintile 12-1M momentum in the 6 trading days before month-end."""

    id          = STRATEGY_ID
    name        = 'IntramontMomentumCycle'
    description = ("Dash-for-cash calendar overlay: long momentum winners / short losers "
                   "only during the 6 intramonth trading days before month-end.")
    tier        = 2
    min_lookback = LOOKBACK_LONG + LOOKBACK_SKIP + 5

    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

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

        # ── 1. Filter to available universe tickers ────────────────────────
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        prices_sub = prices[tickers].sort_index()
        all_dates  = prices_sub.index

        if len(all_dates) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        today = all_dates[-1]

        # ── 2. Calendar filter: active only in last 6 trading days of month ──
        days_left = _trading_days_until_month_end(today, all_dates)
        if days_left > ACTIVE_DAYS:
            # FLAT window — no signals
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── 3. Cross-sectional 12-1M momentum score ───────────────────────
        # momentum = cumulative return from t-252 to t-21
        if len(all_dates) < LOOKBACK_LONG + 1:
            print('[debug] signals=0', file=sys.stderr)
            return []

        idx_long  = -LOOKBACK_LONG   # ~12 months ago
        idx_skip  = -LOOKBACK_SKIP   # ~1 month ago (skip last month)

        prices_long = prices_sub.iloc[idx_long]
        prices_skip = prices_sub.iloc[idx_skip]
        prices_now  = prices_sub.iloc[-1]

        # Require valid prices at both ends
        valid = (prices_long > 0) & (prices_skip > 0) & (prices_now > 0)
        valid_tickers = [t for t in tickers if valid.get(t, False)]

        if len(valid_tickers) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        momentum_score = (prices_skip[valid_tickers] / prices_long[valid_tickers]) - 1.0
        ranked = momentum_score.rank(pct=True)

        top_cutoff    = 1.0 - QUINTILE
        bottom_cutoff = QUINTILE

        long_tickers  = ranked[ranked >= top_cutoff].index.tolist()
        short_tickers = ranked[ranked <= bottom_cutoff].index.tolist()

        if not long_tickers and not short_tickers:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale   = self.position_scale(regime_state)
        signals = []

        for ticker in long_tickers[:self.MAX_SIGNALS // 2]:
            price_series = prices_sub[ticker].dropna()
            if len(price_series) < 14:
                continue
            current_price = float(price_series.iloc[-1])
            stops = self.compute_stops_and_targets(
                price_series, 'LONG', current_price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(BASE_SIZE * scale, 4),
                confidence        = 'MED',
                signal_params     = {
                    'momentum_score': round(float(momentum_score[ticker]), 4),
                    'days_until_month_end': days_left,
                    'regime': regime_state,
                },
            ))

        for ticker in short_tickers[:self.MAX_SIGNALS // 2]:
            price_series = prices_sub[ticker].dropna()
            if len(price_series) < 14:
                continue
            current_price = float(price_series.iloc[-1])
            stops = self.compute_stops_and_targets(
                price_series, 'SHORT', current_price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(BASE_SIZE * scale, 4),
                confidence        = 'MED',
                signal_params     = {
                    'momentum_score': round(float(momentum_score[ticker]), 4),
                    'days_until_month_end': days_left,
                    'regime': regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
