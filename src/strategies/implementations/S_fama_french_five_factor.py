"""
S_fama_french_five_factor — Fama & French (2015) RFS
LONG top-quintile of (RMW + CMA) composite, SHORT bottom-quintile.
OP  = (Revenue - COGS - SGA - Interest) / Book_Equity
INV = total_asset_growth  (low = conservative investment = high CMA rank)
Annual rebalance using prior fiscal-year fundamentals; equal-weight within quintile.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['FamaFrenchFiveFactor']


class FamaFrenchFiveFactor(BaseStrategy):
    """RMW+CMA composite rank: LONG top quintile, SHORT bottom quintile — Fama & French (2015)."""

    id               = 'S_fama_french_five_factor'
    name             = 'FamaFrenchFiveFactor'
    description      = 'RMW+CMA composite rank: LONG top quintile, SHORT bottom quintile — Fama & French (2015)'
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

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Step 1: compute OP and INV for each ticker ──────────────────────
        scores: dict[str, dict] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue

            revenue  = fin.get('revenue') or fin.get('totalRevenue') or 0.0
            cogs     = fin.get('costOfRevenue') or fin.get('costOfGoodsSold') or 0.0
            sga      = (fin.get('sellingGeneralAndAdministrativeExpenses')
                        or fin.get('sellingAndMarketingExpenses')
                        or fin.get('operatingExpenses') or 0.0)
            interest = abs(fin.get('interestExpense') or 0.0)
            book_eq  = (fin.get('totalStockholdersEquity')
                        or fin.get('totalEquity')
                        or fin.get('bookValue') or 0.0)

            if book_eq <= 0 or revenue <= 0:
                continue

            op = float(revenue - cogs - sga - interest) / float(book_eq)

            total_assets = fin.get('totalAssets') or 0.0
            if total_assets <= 0:
                continue

            # Prefer explicit asset-growth field; otherwise default to 0 (median rank)
            asset_growth = fin.get('totalAssetsGrowth') or fin.get('assetGrowth')
            inv = float(asset_growth) if asset_growth is not None else 0.0

            scores[ticker] = {'op': op, 'inv': inv}

        if len(scores) < 10:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: cross-sectional percentile ranks ─────────────────────────
        tickers = list(scores.keys())
        ops     = np.array([scores[t]['op']  for t in tickers], dtype=float)
        invs    = np.array([scores[t]['inv'] for t in tickers], dtype=float)

        op_rank  = pd.Series(ops).rank(pct=True).to_numpy()
        inv_rank = pd.Series(-invs).rank(pct=True).to_numpy()   # low INV → high rank

        composite = op_rank + inv_rank   # range [~0, ~2]

        # ── Step 3: quintile selection ───────────────────────────────────────
        n           = len(tickers)
        q_cut       = max(n // 5, 1)
        q_cut       = min(q_cut, self.MAX_SIGNALS // 2)   # stay under MAX_SIGNALS cap
        sorted_idx  = np.argsort(composite)
        bottom_idx  = sorted_idx[:q_cut]    # SHORT — low OP + high INV (aggressive expanders)
        top_idx     = sorted_idx[-q_cut:]   # LONG  — high OP + low INV (profitable conservatives)

        # Equal-weight; cap individual position at 5%
        per_pos = min(round(scale / max(len(top_idx), 1), 4), 0.05)

        signals: List[Signal] = []

        for idx in top_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            c = float(composite[idx])
            conf = 'HIGH' if c > 1.6 else ('MED' if c > 1.3 else 'LOW')
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
                    'op_rank':   round(float(op_rank[idx]),  4),
                    'inv_rank':  round(float(inv_rank[idx]), 4),
                    'composite': round(c,                    4),
                },
            ))

        for idx in bottom_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            c = float(composite[idx])
            conf = 'HIGH' if c < 0.4 else ('MED' if c < 0.7 else 'LOW')
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
                    'op_rank':   round(float(op_rank[idx]),  4),
                    'inv_rank':  round(float(inv_rank[idx]), 4),
                    'composite': round(c,                    4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
