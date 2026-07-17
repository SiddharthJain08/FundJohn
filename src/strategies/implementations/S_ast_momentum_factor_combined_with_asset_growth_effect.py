"""
S_ast_momentum_factor_combined_with_asset_growth_effect
Source: https://quantpedia.com/strategies/momentum-factor-combined-with-asset-growth-effect/

Universe: 500 most liquid US equities (NYSE/AMEX/NASDAQ) by dollar volume, with fundamental data.
Monthly rebalance. Step 1: rank all stocks by YoY total-asset growth; keep top decile (highest
asset growth). Step 2: within that decile, rank by 11-month momentum (t-12 to t-2, skipping the
most recent month); go LONG the top quintile and SHORT the bottom quintile. Equal-weight both legs.
January excluded (momentum reversal month). 5x leverage proxy via position_size_pct.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['MomentumFactorCombinedWithAssetGrowthEffect']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_ast_momentum_factor_combined_with_asset_growth_effect'


class MomentumFactorCombinedWithAssetGrowthEffect(BaseStrategy):
    """Long top-quintile / short bottom-quintile 11-month momentum within the highest-asset-growth decile; January excluded; monthly rebalance."""

    id                = STRATEGY_ID
    name              = 'MomentumFactorCombinedWithAssetGrowthEffect'
    description       = (
        'Stocks with the highest asset growth (top decile) and strongest 11-month momentum '
        '(top quintile, skipping last month) outperform those with weak momentum within the '
        'same high-growth cohort, especially outside January.'
    )
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 390  # ~13 months of daily bars
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

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
            return []

        latest_date = prices.index[-1]

        # January exclusion — momentum historically reverses in January
        if hasattr(latest_date, 'month') and latest_date.month == 1:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Monthly rebalance: only fire on the last trading day of the current month
        current_month = latest_date.month
        current_year = latest_date.year
        month_dates = [d for d in prices.index if d.month == current_month and d.year == current_year]
        if not month_dates or latest_date != month_dates[-1]:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Step 1: compute YoY asset growth for each ticker ─────────────────
        asset_growth_map: dict[str, float] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue
            total_assets_curr = fin.get('totalAssets')
            total_assets_prev = (
                fin.get('totalAssetsPriorYear')
                or fin.get('totalAssetsPY')
                or fin.get('totalAssetsPreviousYear')
            )
            if total_assets_curr is None or total_assets_prev is None:
                continue
            try:
                curr = float(total_assets_curr)
                prev = float(total_assets_prev)
            except (TypeError, ValueError):
                continue
            if prev <= 0 or curr <= 0:
                continue
            growth = (curr - prev) / prev
            if not np.isfinite(growth):
                continue
            asset_growth_map[ticker] = growth

        if len(asset_growth_map) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Keep the top decile (highest asset growth)
        tickers_sorted_by_growth = sorted(
            asset_growth_map.keys(), key=lambda t: asset_growth_map[t], reverse=True
        )
        decile_count = max(len(tickers_sorted_by_growth) // 10, 1)
        top_decile = tickers_sorted_by_growth[:decile_count]

        if len(top_decile) < 5:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: compute 11-month momentum (t-12 to t-2) within top decile ──
        monthly = prices.resample('ME').last()
        if len(monthly) < 13:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        momentum_map: dict[str, float] = {}
        for ticker in top_decile:
            if ticker not in monthly.columns:
                continue
            ts = monthly[ticker].dropna()
            if len(ts) < 13:
                continue
            # Skip last month: use iloc[-2] as end, iloc[-13] as start
            p_end = float(ts.iloc[-2])    # t-1 close (end of 11-month window)
            p_start = float(ts.iloc[-13]) # t-12 close (start of window)
            if p_start <= 0 or not np.isfinite(p_end) or not np.isfinite(p_start):
                continue
            mom = (p_end / p_start) - 1.0
            if np.isfinite(mom):
                momentum_map[ticker] = mom

        if len(momentum_map) < 5:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Sort by 11-month momentum, pick top and bottom quintiles
        mom_sorted = sorted(momentum_map.keys(), key=lambda t: momentum_map[t], reverse=True)
        quintile_count = max(len(mom_sorted) // 5, 1)
        long_tickers = mom_sorted[:quintile_count]
        short_tickers = mom_sorted[-quintile_count:]

        # Equal-weight within each leg; cap per position
        n_long = max(len(long_tickers), 1)
        n_short = max(len(short_tickers), 1)
        per_long = min(round(scale * 0.25 / n_long, 4), 0.10)
        per_short = min(round(scale * 0.25 / n_short, 4), 0.10)

        signals: List[Signal] = []

        for i, ticker in enumerate(long_tickers):
            if ticker not in prices.columns:
                continue
            ts = prices[ticker].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            rank_pct = i / max(n_long - 1, 1)
            conf = 'HIGH' if rank_pct < 0.20 else ('MED' if rank_pct < 0.50 else 'LOW')
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_long,
                confidence        = conf,
                signal_params     = {
                    'asset_growth': round(float(asset_growth_map[ticker]), 4),
                    'momentum_11m': round(float(momentum_map[ticker]), 4),
                    'strategy':     STRATEGY_ID,
                },
            ))

        for i, ticker in enumerate(short_tickers):
            if ticker not in prices.columns:
                continue
            ts = prices[ticker].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            # short_tickers are the worst momentum stocks (bottom quintile)
            rank_pct = (n_short - 1 - i) / max(n_short - 1, 1)  # 1.0 = worst
            conf = 'HIGH' if rank_pct > 0.80 else ('MED' if rank_pct > 0.50 else 'LOW')
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_short,
                confidence        = conf,
                signal_params     = {
                    'asset_growth': round(float(asset_growth_map[ticker]), 4),
                    'momentum_11m': round(float(momentum_map[ticker]), 4),
                    'strategy':     STRATEGY_ID,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
