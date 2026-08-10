"""
S_ast_asset_growth_effect — Asset Growth Effect (Cooper, Gulen & Schill 2008)
Source: https://quantpedia.com/strategies/asset-growth-effect/

LONG low-asset-growth decile / SHORT high-asset-growth decile.
Annual rebalance triggered at end of June using prior fiscal-year total assets.
Hypothesis: rapid asset expansion signals overinvestment and dilutes returns on capital.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AssetGrowthEffect']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_ast_asset_growth_effect'


class AssetGrowthEffect(BaseStrategy):
    """LONG low-asset-growth decile / SHORT high-asset-growth decile (annual June rebalance)."""

    id               = 'S_ast_asset_growth_effect'
    name             = 'AssetGrowthEffect'
    description      = 'Firms with low total-asset growth outperform firms with high total-asset growth because rapid asset expansion signals overinvestment'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

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

        # Annual June rebalance: fire on the FIRST bar inside June (previous
        # bar is not June) — once per year under daily AND monthly sampling,
        # instead of every June bar under daily iteration.
        latest_date = prices.index[-1]
        if hasattr(latest_date, 'month'):
            prev_date = prices.index[-2] if len(prices.index) >= 2 else None
            into_june = latest_date.month == 6 and (
                prev_date is None or not hasattr(prev_date, 'month')
                or prev_date.month != 6)
            if not into_june:
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

        # ── Step 2: rank and select deciles ──────────────────────────────────
        tickers   = list(asset_growth_map.keys())
        growths   = np.array([asset_growth_map[t] for t in tickers], dtype=float)
        ranks_pct = pd.Series(growths).rank(pct=True).to_numpy()  # 0=lowest, 1=highest

        n         = len(tickers)
        decile    = max(n // 10, 1)
        decile    = min(decile, self.MAX_SIGNALS // 2)

        sorted_idx = np.argsort(ranks_pct)
        long_idx   = sorted_idx[:decile]    # LONG: lowest asset growth
        short_idx  = sorted_idx[-decile:]   # SHORT: highest asset growth

        # Equal-weight within each leg
        per_pos = min(round(scale / max(decile, 1), 4), 0.10)

        signals: List[Signal] = []

        for idx in long_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            pct   = float(ranks_pct[idx])
            conf  = 'HIGH' if pct < 0.05 else ('MED' if pct < 0.10 else 'LOW')
            signals.append(Signal(
                ticker            = t,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_pos,
                confidence        = conf,
                signal_params     = {
                    'asset_growth':     round(float(asset_growth_map[t]), 4),
                    'asset_growth_pct': round(pct, 4),
                    'strategy':         STRATEGY_ID,
                },
            ))

        for idx in short_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            pct   = float(ranks_pct[idx])
            conf  = 'HIGH' if pct > 0.95 else ('MED' if pct > 0.90 else 'LOW')
            signals.append(Signal(
                ticker            = t,
                direction         = 'SHORT',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_pos,
                confidence        = conf,
                signal_params     = {
                    'asset_growth':     round(float(asset_growth_map[t]), 4),
                    'asset_growth_pct': round(pct, 4),
                    'strategy':         STRATEGY_ID,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
