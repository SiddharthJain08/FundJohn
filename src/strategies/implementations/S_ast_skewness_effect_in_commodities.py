"""
Skewness Effect in Commodities — ETF proxy implementation.

Source: https://quantpedia.com/strategies/skewness-effect-in-commodities/
Reference: Bali, Brown & Murray (2017), "A Lottery Demand-Based Explanation of the Beta Anomaly."

Original universe: 27 CME/ICE continuous commodity futures (grains, metals, energy,
softs, livestock). This implementation proxies via commodity ETFs in prices.parquet.

Monthly rebalance at month-start. 252-day log-return skewness ranked descending;
LONG the bottom quintile (most negatively skewed — highest expected return per skewness
premium); FLAT the top quintile (most positively skewed). ETP class — no short leg.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd
from scipy.stats import skew as _scipy_skew

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstSkewnessEffectInCommodities']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_ast_skewness_effect_in_commodities'

# Commodity ETP proxy basket — covers grains, metals, energy, softs, broad baskets
BASKET = [
    'GLD',  # gold
    'SLV',  # silver
    'PPLT', # platinum
    'PALL', # palladium
    'CPER', # copper
    'USO',  # WTI crude oil
    'BNO',  # Brent crude
    'UNG',  # natural gas
    'DBA',  # diversified agriculture
    'CORN', # corn
    'WEAT', # wheat
    'SOYB', # soybeans
    'NIB',  # cocoa
    'JO',   # coffee
    'SGG',  # sugar
    'GSG',  # broad commodity index
]

LOOKBACK = 252  # ~12 months of trading days
QUANTILE = 5    # quintile sort (top/bottom 20%)


class AstSkewnessEffectInCommodities(BaseStrategy):
    """Commodity skewness-effect strategy via ETF proxies.

    Ranks commodity ETFs by 252-day log-return skewness monthly. Goes LONG
    the bottom quintile (lowest/most-negative skewness — higher expected return
    per the skewness premium). FLATs the top quintile. Equal-weight within
    the long leg, regime-scaled.

    Source: https://quantpedia.com/strategies/skewness-effect-in-commodities/
    """

    id                = STRATEGY_ID
    name              = 'Skewness Effect in Commodities (ETP)'
    description       = (
        'Monthly quintile sort on 252-day log-return skewness across commodity ETFs; '
        'long bottom quintile (most negatively skewed), flat top quintile.'
    )
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = LOOKBACK + 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 20

    def default_parameters(self) -> dict:
        return {
            'lookback':      LOOKBACK,
            'quantile':      QUANTILE,
            'pos_size_base': 0.08,  # base total allocation to long leg
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Monthly rebalance gate — only fire on first trading day of each month
        if len(prices) < 2:
            print(f'[debug] signals=0', file=sys.stderr)
            return []
        if prices.index[-1].month == prices.index[-2].month:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        lookback = self.parameters.get('lookback', LOOKBACK)
        quantile = self.parameters.get('quantile', QUANTILE)

        if len(prices) < lookback + 1:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        available = [t for t in BASKET if t in prices.columns]
        if not available:
            print(f'[{STRATEGY_ID}] no basket tickers in prices', file=sys.stderr)
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Compute 252-day log-return skewness for each available ticker
        skewness_map: dict[str, float] = {}
        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < lookback + 1:
                continue
            price_slice = series.iloc[-(lookback + 1):]
            log_rets = np.log(price_slice / price_slice.shift(1)).dropna()
            if len(log_rets) < lookback // 2:
                continue
            sk = float(_scipy_skew(log_rets))
            if np.isfinite(sk):
                skewness_map[ticker] = sk

        if len(skewness_map) < quantile:
            print(f'[{STRATEGY_ID}] only {len(skewness_map)} tickers < quantile={quantile}', file=sys.stderr)
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Sort ascending by skewness
        sorted_by_skew = sorted(skewness_map.items(), key=lambda x: x[1])
        q_size = max(1, len(sorted_by_skew) // quantile)

        long_tickers = [t for t, _ in sorted_by_skew[:q_size]]    # bottom quintile → LONG
        flat_tickers = [t for t, _ in sorted_by_skew[-q_size:]]   # top quintile → FLAT

        scale         = self.position_scale(regime_state)
        pos_size_base = self.parameters.get('pos_size_base', 0.08)
        pos_size_each = float(round(pos_size_base / max(1, len(long_tickers)) * scale, 4))

        signals: List[Signal] = []

        for ticker in long_tickers:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            current_price = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=current_price, regime_state=regime_state,
            )
            sk_val = skewness_map[ticker]
            if sk_val < -0.5:
                confidence = 'HIGH'
            elif sk_val < 0.0:
                confidence = 'MED'
            else:
                confidence = 'LOW'
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = float(stops['stop']),
                target_1          = float(stops['t1']),
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = pos_size_each,
                confidence        = confidence,
                signal_params     = {
                    'skewness': round(sk_val, 4),
                    'quintile': 'bottom',
                    'regime':   regime_state,
                    'scale':    scale,
                    'lookback': lookback,
                    'q_size':   q_size,
                },
            ))

        for ticker in flat_tickers:
            series = prices[ticker].dropna()
            if series.empty:
                continue
            current_price = float(series.iloc[-1])
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'FLAT',
                entry_price       = current_price,
                stop_loss         = float(current_price * 0.95),
                target_1          = float(current_price * 1.05),
                target_2          = float(current_price * 1.10),
                target_3          = float(current_price * 1.15),
                position_size_pct = 0.0,
                confidence        = 'LOW',
                signal_params     = {
                    'skewness': round(skewness_map[ticker], 4),
                    'quintile': 'top',
                    'regime':   regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
