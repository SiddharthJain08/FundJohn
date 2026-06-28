"""
Value and Momentum Factors Across Asset Classes — ported from Quantpedia / awesome-systematic-trading.

Monthly rebalance across a fixed 11-ETF multi-asset basket (SPY, MDY, IYR, EWU, EWJ,
EEM, IEF, LQD, HYG, BWX, BIL).  Ranks each ETF by the classic 12-1 momentum score
(12-month total return minus 1-month total return, cross-sectionally z-scored).  Goes
long the top one-third of the ranked basket, equal-weight, held for one month.

NOTE: the original paper (Asness, Moskowitz & Pedersen 2013) also uses a value component
(earnings yield for equities, YTM for bonds) which requires proprietary Quantpedia /
FRED yield feeds unavailable in this system.  This implementation is momentum-only as
documented in the strategy spec; the value signal can be added once a yield feed is wired.

Source: https://quantpedia.com/strategies/value-and-momentum-factors-across-asset-classes/
"""
from __future__ import annotations

import sys
import pandas as pd
import numpy as np
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstValueAndMomentumFactorsAcrossAssetClasses']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID = 'S_ast_value_and_momentum_factors_across_asset_classes'

# Fixed asset-class ETF basket
BASKET = ['SPY', 'MDY', 'IYR', 'EWU', 'EWJ', 'EEM', 'IEF', 'LQD', 'HYG', 'BWX', 'BIL']

LOOKBACK_12M = 252  # ~12 months of trading days
LOOKBACK_1M  = 21   # ~1 month of trading days


class AstValueAndMomentumFactorsAcrossAssetClasses(BaseStrategy):
    """Monthly momentum-only rotation across 11 multi-asset ETFs (12-1m lookback).

    Ranks each basket member by 12-month minus 1-month total return (cross-sectionally
    z-scored).  Goes long the top one-third by rank, equal-weight, rebalancing on the
    first trading day of each new calendar month.
    """

    id               = STRATEGY_ID
    name             = 'Value and Momentum Factors Across Asset Classes'
    description      = (
        'Monthly rebalance across 11 asset-class ETFs; '
        'ranks by 12-1m momentum z-score; long top one-third equal-weight.'
    )
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = LOOKBACK_12M + LOOKBACK_1M
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS      = 5

    def default_parameters(self) -> dict:
        return {
            'lookback_12m':   LOOKBACK_12M,
            'lookback_1m':    LOOKBACK_1M,
            'long_frac':      1 / 3,   # fraction of available basket to go long
            'pos_size_base':  0.08,    # per-position base size fraction pre-regime-scale
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

        # Only rebalance on the first trading day of a new calendar month.
        all_dates = prices.index
        if len(all_dates) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []
        if all_dates[-1].month == all_dates[-2].month:
            print('[debug] signals=0', file=sys.stderr)
            return []

        lookback_12m = int(self.parameters.get('lookback_12m', LOOKBACK_12M))
        lookback_1m  = int(self.parameters.get('lookback_1m',  LOOKBACK_1M))

        if len(prices) < lookback_12m:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Restrict to basket tickers present in the price panel.
        available = [t for t in BASKET if t in prices.columns]
        if len(available) < 3:
            print(f'[{STRATEGY_ID}] only {len(available)} basket tickers in panel (<3) — skip', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Compute 12-1 momentum for each ticker.
        scores: dict[str, float] = {}
        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < lookback_12m:
                continue
            try:
                p_now  = float(series.iloc[-1])
                p_12m  = float(series.iloc[-lookback_12m])
                p_1m   = float(series.iloc[-lookback_1m])
                if p_12m <= 0 or p_1m <= 0:
                    continue
                ret_12m = p_now / p_12m - 1.0
                ret_1m  = p_now / p_1m  - 1.0
                scores[ticker] = ret_12m - ret_1m  # classic 12-1 momentum
            except (ZeroDivisionError, ValueError, IndexError):
                continue

        if len(scores) < 3:
            print(f'[{STRATEGY_ID}] fewer than 3 scoreable tickers — skip', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Cross-sectional z-score normalisation.
        score_s = pd.Series(scores, dtype=float)
        std = score_s.std()
        score_z = (score_s - score_s.mean()) / std if std > 1e-9 else score_s * 0.0

        # Select top one-third by rank.
        long_frac = float(self.parameters.get('long_frac', 1 / 3))
        n_long    = max(1, round(len(score_z) * long_frac))
        top_tickers = score_z.nlargest(n_long).index.tolist()

        scale         = self.position_scale(regime_state)
        pos_size_base = float(self.parameters.get('pos_size_base', 0.08))
        pos_size      = round(pos_size_base * scale, 4)

        signals: List[Signal] = []
        for ticker in top_tickers:
            series = prices[ticker].dropna()
            current_price = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series,
                direction='LONG',
                current_price=current_price,
                regime_state=regime_state,
            )
            z_val = float(score_z[ticker])
            if z_val > 1.0:
                confidence = 'HIGH'
            elif z_val > 0.0:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'momentum_z':  round(z_val, 4),
                    'n_long':      n_long,
                    'regime':      regime_state,
                    'scale':       scale,
                    'all_scores':  {t: round(float(score_z[t]), 4) for t in score_z.index},
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
