"""
S_ast_roa_effect_within_stocks — Quantpedia
ROA Effect Within Stocks: LONG high-ROA / SHORT low-ROA within each market-cap half.
Source: https://quantpedia.com/strategies/roa-effect-within-stocks/

Universe split into large-cap and small-cap halves; within each half stocks are
ranked by trailing quarterly ROA (net income / lagged total assets). Long top 3
deciles, short bottom 3 deciles from each half. Monthly rebalance.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import no_otc as universe_filter

INSTRUMENT_CLASS = "equity"

__all__ = ['AstROAEffectWithinStocks']


class AstROAEffectWithinStocks(BaseStrategy):
    """LONG high-ROA / SHORT low-ROA within large-cap and small-cap halves (Quantpedia ROA Effect)."""

    id               = 'S_ast_roa_effect_within_stocks'
    name             = 'AstROAEffectWithinStocks'
    description      = 'Stocks with high ROA outperform stocks with low ROA within each market-cap half, generating alpha from a long-short spread rebalanced monthly.'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 63
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

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

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Compute ROA, market cap, and sales for each ticker ─────────────────
        candidates: list[dict] = []
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue

            ts = prices[ticker].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            if price < 5.0:
                continue

            # Sales > $10M filter
            sales = fin.get('revenue') or fin.get('totalRevenue') or 0
            if not sales or float(sales) < 10_000_000:
                continue

            # Market cap (for large/small split)
            mktcap = fin.get('marketCap')
            if not mktcap:
                continue
            mktcap = float(mktcap)
            if mktcap <= 0:
                continue

            # ROA = quarterly net income / one-quarter-lagged total assets
            net_income = fin.get('netIncome')
            if net_income is None:
                continue
            net_income = float(net_income)

            lagged_assets = (
                fin.get('totalAssetsPriorYear')
                or fin.get('totalAssetsPY')
                or fin.get('totalAssetsPreviousYear')
                or fin.get('totalAssets')
            )
            if not lagged_assets:
                continue
            lagged_assets = float(lagged_assets)
            if lagged_assets <= 0:
                continue

            roa = net_income / lagged_assets
            if not np.isfinite(roa) or roa == 0.0:
                continue

            candidates.append({
                'ticker': ticker,
                'roa':    roa,
                'mktcap': mktcap,
                'price':  price,
                'ts':     ts,
            })

        if len(candidates) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Split into large-cap and small-cap halves by market cap ────────────
        candidates.sort(key=lambda x: x['mktcap'], reverse=True)
        half = len(candidates) // 2
        groups = [candidates[:half], candidates[half:]]

        signals: List[Signal] = []

        for group in groups:
            n = len(group)
            if n < 10:
                continue

            # Sort by ROA descending for decile selection
            group_sorted = sorted(group, key=lambda x: x['roa'], reverse=True)
            decile = max(n // 10, 1)
            long_set  = group_sorted[:decile * 3]
            short_set = group_sorted[-(decile * 3):]

            n_long  = len(long_set)
            n_short = len(short_set)
            per_long  = min(round(scale / max(n_long,  1), 4), 0.04)
            per_short = min(round(scale / max(n_short, 1), 4), 0.04)

            for i, rec in enumerate(long_set):
                t     = rec['ticker']
                ts    = rec['ts']
                price = rec['price']
                stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
                rank_frac = i / n_long
                conf  = 'HIGH' if rank_frac < 0.10 else ('MED' if rank_frac < 0.30 else 'LOW')
                signals.append(Signal(
                    ticker            = t,
                    direction         = 'LONG',
                    entry_price       = price,
                    stop_loss         = stops['stop'],
                    target_1          = stops['t1'],
                    target_2          = stops['t2'],
                    target_3          = stops['t3'],
                    position_size_pct = per_long,
                    confidence        = conf,
                    signal_params     = {
                        'roa':      round(rec['roa'],    6),
                        'mktcap':   round(rec['mktcap'], 0),
                        'strategy': 'roa_effect_within_stocks',
                    },
                ))

            for i, rec in enumerate(short_set):
                t     = rec['ticker']
                ts    = rec['ts']
                price = rec['price']
                stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
                rank_frac = i / n_short
                conf  = 'HIGH' if rank_frac < 0.10 else ('MED' if rank_frac < 0.30 else 'LOW')
                signals.append(Signal(
                    ticker            = t,
                    direction         = 'SHORT',
                    entry_price       = price,
                    stop_loss         = stops['stop'],
                    target_1          = stops['t1'],
                    target_2          = stops['t2'],
                    target_3          = stops['t3'],
                    position_size_pct = per_short,
                    confidence        = conf,
                    signal_params     = {
                        'roa':      round(rec['roa'],    6),
                        'mktcap':   round(rec['mktcap'], 0),
                        'strategy': 'roa_effect_within_stocks',
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
