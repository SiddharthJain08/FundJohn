"""
S_price_path_convexity — Cross-sectional price-path convexity strategy.

Signal: second_derivative(price_path_t-60:t) ranked across universe.
LONG bottom quintile (low/negative convexity), SHORT top quintile (high convexity).
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['PricePathConvexity']

LOOKBACK = 60
SMOOTH_WIN = 5


class PricePathConvexity(BaseStrategy):
    """Rank SP500 by 60-day price-path convexity; long concave, short convex."""

    id          = 'S_price_path_convexity'
    name        = 'PricePathConvexity'
    description = 'second_derivative(60d price) cross-sectional rank; LONG low-convexity, SHORT high-convexity'
    tier        = 2
    min_lookback = LOOKBACK + SMOOTH_WIN

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # Filter universe to available columns
        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Need at least LOOKBACK + SMOOTH_WIN rows
        if len(prices) < self.min_lookback:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Compute convexity scores for each ticker
        convexity_scores: dict = {}
        window = prices.iloc[-self.min_lookback:]

        for ticker in tickers:
            series = window[ticker].dropna()
            if len(series) < LOOKBACK:
                continue
            # Smooth to reduce microstructure noise
            smoothed = series.rolling(SMOOTH_WIN, min_periods=SMOOTH_WIN).mean().dropna()
            if len(smoothed) < LOOKBACK:
                continue
            # Use last LOOKBACK smoothed values
            s = smoothed.iloc[-LOOKBACK:].values
            # Second derivative via central-difference midpoint approximation:
            #   d2 ≈ (s[-1] - 2*s[LOOKBACK//2] + s[0]) / (LOOKBACK/2)^2
            # Normalized by price level so scores are comparable across tickers
            mid = s[LOOKBACK // 2]
            price_level = float(s[-1]) if float(s[-1]) != 0 else 1.0
            d2 = (s[-1] - 2.0 * mid + s[0]) / ((LOOKBACK / 2) ** 2) / price_level
            convexity_scores[ticker] = float(d2)

        if not convexity_scores:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Rank by convexity
        scored = sorted(convexity_scores.items(), key=lambda x: x[1])
        n = len(scored)
        quintile = max(1, n // 5)

        longs  = [t for t, _ in scored[:quintile]]            # low convexity
        shorts = [t for t, _ in scored[n - quintile:]]        # high convexity

        # Cap total signals at MAX_SIGNALS
        max_per_side = self.MAX_SIGNALS // 2
        longs  = longs[:max_per_side]
        shorts = shorts[:max_per_side]

        # Base position size: split equally within each side, capped at 5% per name
        n_signals = len(longs) + len(shorts)
        if n_signals == 0:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        base_size = min(0.05, scale / max(n_signals, 1))

        last_prices = prices.iloc[-1]
        signals: List[Signal] = []

        for ticker in longs:
            if ticker not in last_prices.index or pd.isna(last_prices[ticker]):
                continue
            price = float(last_prices[ticker])
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(),
                'LONG',
                price,
                regime_state=regime_state,
            )
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = price * (1.0 + 0.03),
                target_2          = price * (1.0 + 0.06),
                target_3          = price * (1.0 + 0.09),
                position_size_pct = base_size,
                confidence        = 'MED',
                signal_params     = {'convexity': convexity_scores[ticker], 'side': 'low_convexity'},
            ))

        for ticker in shorts:
            if ticker not in last_prices.index or pd.isna(last_prices[ticker]):
                continue
            price = float(last_prices[ticker])
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(),
                'SHORT',
                price,
                regime_state=regime_state,
            )
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = price * (1.0 - 0.03),
                target_2          = price * (1.0 - 0.06),
                target_3          = price * (1.0 - 0.09),
                position_size_pct = base_size,
                confidence        = 'MED',
                signal_params     = {'convexity': convexity_scores[ticker], 'side': 'high_convexity'},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
