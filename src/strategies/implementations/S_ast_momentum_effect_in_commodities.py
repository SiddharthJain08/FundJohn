"""
Momentum Effect in Commodities — ETF proxy implementation.

Source: https://quantpedia.com/strategies/1-month-momentum-in-commodities/
Reference: Quantpedia / Gorton, Hayashi & Rouwenhorst (2013), "The Fundamentals of Commodity Futures Returns."

Original universe: 30 CME/ICE continuous commodity futures (grains, metals, energy,
softs, livestock). This implementation proxies via commodity ETFs in prices.parquet.

Monthly rebalance at month-start. 252-day (12-month) rate-of-change ranked descending;
LONG the top quintile (highest momentum); SHORT the bottom quintile (lowest momentum).
Equal-weight within each leg. Regime-scaled position sizes.
"""
from __future__ import annotations

import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstMomentumEffectInCommodities']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_ast_momentum_effect_in_commodities'

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


class AstMomentumEffectInCommodities(BaseStrategy):
    """12-month momentum quintile long-short across commodity ETF proxies.

    Monthly rebalance: rank commodity ETFs by 252-day rate-of-change. Go LONG
    the top quintile (highest momentum) and SHORT the bottom quintile (lowest
    momentum). Equal-weight within each leg. Regime-scaled.

    Source: https://quantpedia.com/strategies/1-month-momentum-in-commodities/
    """

    id                = STRATEGY_ID
    name              = 'Momentum Effect in Commodities (ETP)'
    description       = (
        'Monthly quintile sort on 252-day ROC across commodity ETF proxies; '
        'long top quintile, short bottom quintile, equal-weight within each leg.'
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
            'pos_size_base': 0.08,   # base total allocation per leg
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

        # Compute 252-day rate-of-change for each available ticker
        roc_map: dict[str, float] = {}
        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < lookback + 1:
                continue
            try:
                past  = float(series.iloc[-(lookback + 1)])
                curr  = float(series.iloc[-1])
                if past <= 0:
                    continue
                roc_map[ticker] = (curr - past) / past
            except (ValueError, ZeroDivisionError):
                continue

        if len(roc_map) < quantile:
            print(f'[{STRATEGY_ID}] only {len(roc_map)} tickers < quantile={quantile}', file=sys.stderr)
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        sorted_by_roc = sorted(roc_map.items(), key=lambda x: x[1], reverse=True)
        q_size = max(1, len(sorted_by_roc) // quantile)

        long_tickers  = [t for t, _ in sorted_by_roc[:q_size]]   # top quintile → LONG
        short_tickers = [t for t, _ in sorted_by_roc[-q_size:]]  # bottom quintile → SHORT

        scale         = self.position_scale(regime_state)
        pos_size_base = self.parameters.get('pos_size_base', 0.08)
        long_pos_each = float(round(pos_size_base / max(1, len(long_tickers))  * scale, 4))
        short_pos_each = float(round(pos_size_base / max(1, len(short_tickers)) * scale, 4))

        signals: List[Signal] = []

        for ticker in long_tickers:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            current_price = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=current_price, regime_state=regime_state,
            )
            mom = roc_map[ticker]
            confidence = 'HIGH' if mom > 0.10 else ('MED' if mom > 0.0 else 'LOW')
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = float(stops['stop']),
                target_1          = float(stops['t1']),
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = long_pos_each,
                confidence        = confidence,
                signal_params     = {
                    'roc_252d': round(mom, 4),
                    'quintile': 'top',
                    'regime':   regime_state,
                    'scale':    scale,
                    'q_size':   q_size,
                },
            ))

        for ticker in short_tickers:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            current_price = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series, direction='SHORT', current_price=current_price, regime_state=regime_state,
            )
            mom = roc_map[ticker]
            confidence = 'HIGH' if mom < -0.10 else ('MED' if mom < 0.0 else 'LOW')
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = current_price,
                stop_loss         = float(stops['stop']),
                target_1          = float(stops['t1']),
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = short_pos_each,
                confidence        = confidence,
                signal_params     = {
                    'roc_252d': round(mom, 4),
                    'quintile': 'bottom',
                    'regime':   regime_state,
                    'scale':    scale,
                    'q_size':   q_size,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
