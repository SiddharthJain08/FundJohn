from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['PriceFilterRuleTrend']


class PriceFilterRuleTrend(BaseStrategy):
    """Filter-rule momentum: LONG when price rises ≥½% from rolling trough; SHORT when falls ≥½% from rolling peak (Sweeney 1988)."""

    id               = 'S_price_filter_rule_trend'
    name             = 'PriceFilterRuleTrend'
    description      = 'Filter-rule momentum: buy ½%-above trough, sell ½%-below peak — Sweeney (1988)'
    tier             = 2
    signal_frequency = 'daily'
    min_lookback     = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    FILTER_PCT   = 0.005   # ½% filter threshold (Sweeney §2)
    LOOKBACK     = 252     # rolling window for trough/peak detection
    BASE_SIZE    = 0.012   # base position size per signal
    TOP_N        = 20      # max signals per direction

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
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

        # Filter to universe tickers present in prices
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 14:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        min_rows = self.LOOKBACK + 5
        if len(prices) < min_rows:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        prices_sub = prices[tickers]
        current_prices = prices_sub.iloc[-1]

        # Compute rolling min/max over lookback window (excluding current bar)
        recent = prices_sub.iloc[-(self.LOOKBACK + 1):-1]
        rolling_min = recent.min()
        rolling_max = recent.max()

        long_signals: list  = []
        short_signals: list = []

        for ticker in tickers:
            raw_price = current_prices.get(ticker)
            if raw_price is None or raw_price != raw_price or raw_price <= 0:
                continue
            price = float(raw_price)

            r_min = float(rolling_min.get(ticker, float('nan')))
            r_max = float(rolling_max.get(ticker, float('nan')))
            if r_min != r_min or r_max != r_max or r_min <= 0 or r_max <= 0:
                continue

            trough_threshold = r_min * (1.0 + self.FILTER_PCT)
            peak_threshold   = r_max * (1.0 - self.FILTER_PCT)

            if price >= trough_threshold:
                # Score = how far above the trigger (momentum strength)
                score = (price / r_min) - 1.0
                long_signals.append((ticker, price, score))
            elif price <= peak_threshold:
                score = 1.0 - (price / r_max)
                short_signals.append((ticker, price, score))

        # Rank by score descending, take top N per direction
        long_signals.sort(key=lambda x: x[2], reverse=True)
        short_signals.sort(key=lambda x: x[2], reverse=True)
        long_signals  = long_signals[:self.TOP_N]
        short_signals = short_signals[:self.TOP_N]

        # Cap to MAX_SIGNALS total (split evenly)
        half_max = self.MAX_SIGNALS // 2
        long_signals  = long_signals[:half_max]
        short_signals = short_signals[:half_max]

        def conf(score: float) -> str:
            if score >= 0.05:
                return 'HIGH'
            elif score >= 0.02:
                return 'MED'
            return 'LOW'

        size = round(self.BASE_SIZE * scale, 6)
        signals: List[Signal] = []

        for ticker, price, score in long_signals:
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size,
                confidence=conf(score),
                signal_params={
                    'filter_pct': self.FILTER_PCT,
                    'rolling_min': round(float(rolling_min[ticker]), 4),
                    'trough_break_pct': round(score, 4),
                },
            ))

        for ticker, price, score in short_signals:
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'SHORT', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size,
                confidence=conf(score),
                signal_params={
                    'filter_pct': self.FILTER_PCT,
                    'rolling_max': round(float(rolling_max[ticker]), 4),
                    'peak_fall_pct': round(score, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
