"""
Asset Class Trend Following — ported from QuantConnect / Quantpedia implementation.

Source: https://quantpedia.com/strategies/asset-class-trend-following/

Hold equal-weight ETF positions only when price is above its 10-month SMA (210 daily bars),
otherwise stay in cash — capturing trend momentum across asset classes while avoiding drawdowns.

Basket: SPY (US equities), EFA (foreign equities), IEF (bonds), VNQ (REITs), GSG (commodities).
Rebalance: first trading day of each month.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AssetClassTrendFollowing']

INSTRUMENT_CLASS = 'etp'

# Fixed 5-ETF basket per Quantpedia spec
BASKET = ('SPY', 'EFA', 'IEF', 'VNQ', 'GSG')

# 10-month SMA period (10 × 21 trading days)
SMA_PERIOD = 210


class AssetClassTrendFollowing(BaseStrategy):
    """Monthly rebalanced trend filter across SPY / EFA / IEF / VNQ / GSG.

    Holds each ETF in equal weight only when its price is above its 210-day SMA;
    emits FLAT for any basket member below SMA to trigger an exit on the
    next execution cycle. Non-rebalance days return an empty list.
    """

    id                = 'S_ast_asset_class_trend_following'
    name              = 'Asset Class Trend Following'
    description       = (
        'Equal-weight ETF basket (SPY/EFA/IEF/VNQ/GSG); hold each only when '
        'price > 210-day SMA, otherwise cash. Monthly rebalance.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = SMA_PERIOD + 1
    # Trend following across asset classes is robust across all regimes;
    # bonds and cash slots provide natural defense in CRISIS.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    MAX_SIGNALS = len(BASKET)

    def default_parameters(self) -> dict:
        return {
            'sma_period':    SMA_PERIOD,
            'pos_size_frac': 0.20,   # base per-ETF fraction (pre-scale; 5 × 0.20 = 100%)
        }

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
            return []

        # Only rebalance on the first trading day of each month.
        # Detected by the last two index entries crossing a month boundary.
        if len(prices.index) < 2:
            return []
        if prices.index[-1].month == prices.index[-2].month:
            return []

        sma_period = self.parameters.get('sma_period', SMA_PERIOD)

        # Need at least sma_period + 1 rows to compute a valid SMA.
        if len(prices) < sma_period + 1:
            print(
                f'[AssetClassTrendFollowing] only {len(prices)} rows < {sma_period + 1} required — returning []',
                file=sys.stderr,
            )
            return []

        scale = self.position_scale(regime_state)

        signals: List[Signal] = []
        eligible_count = 0

        # First pass: determine how many ETFs are eligible (above SMA).
        eligible_flags: dict[str, bool] = {}
        for ticker in BASKET:
            if ticker not in prices.columns:
                eligible_flags[ticker] = False
                continue
            series = prices[ticker].dropna()
            if len(series) < sma_period:
                eligible_flags[ticker] = False
                continue
            sma_val = float(series.iloc[-sma_period:].mean())
            current_price = float(series.iloc[-1])
            eligible_flags[ticker] = current_price > sma_val

        eligible_count = sum(1 for v in eligible_flags.values() if v)

        # Build signals for each basket member.
        for ticker in BASKET:
            if ticker not in prices.columns:
                continue
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue  # not enough data for ATR stop computation
            current_price = float(series.iloc[-1])

            if eligible_flags.get(ticker, False) and eligible_count > 0:
                # Equal weight across eligible ETFs; scale by regime.
                weight = (1.0 / eligible_count) * scale
                weight = round(weight, 4)

                stops = self.compute_stops_and_targets(
                    series,
                    direction='LONG',
                    current_price=current_price,
                    regime_state=regime_state,
                )
                confidence = 'HIGH' if weight >= 0.15 else 'MED'
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = 'LONG',
                    entry_price       = current_price,
                    stop_loss         = stops['stop'],
                    target_1          = stops['t1'],
                    target_2          = stops['t2'],
                    target_3          = stops['t3'],
                    position_size_pct = weight,
                    confidence        = confidence,
                    signal_params     = {
                        'sma_period':    sma_period,
                        'eligible_count': eligible_count,
                        'regime':        regime_state,
                        'scale':         scale,
                    },
                ))
            else:
                # ETF is below SMA — emit FLAT to trigger exit.
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = 'FLAT',
                    entry_price       = current_price,
                    stop_loss         = current_price * 0.95,
                    target_1          = current_price,
                    target_2          = current_price,
                    target_3          = current_price,
                    position_size_pct = 0.0,
                    confidence        = 'LOW',
                    signal_params     = {
                        'sma_period': sma_period,
                        'reason':     'below_sma',
                        'regime':     regime_state,
                    },
                ))

        print(
            f'[AssetClassTrendFollowing] rebalance eligible={eligible_count}/{len(BASKET)} '
            f'signals={len(signals)} regime={regime_state}',
            file=sys.stderr,
        )
        return signals
