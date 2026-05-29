"""
S_book_to_market_value_premium — Davis, Fama & French (2000) JF
High B/M (value) stocks earn a robust premium over low B/M (growth) stocks.
LONG top quintile by book-to-market ratio, SHORT bottom quintile.
Annual rebalance using prior fiscal-year fundamentals.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import r3000 as universe_filter

__all__ = ['BookToMarketValuePremium']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_book_to_market_value_premium'


class BookToMarketValuePremium(BaseStrategy):
    """B/M rank: LONG top quintile (value), SHORT bottom quintile (growth) — Davis, Fama & French (2000)."""

    id               = STRATEGY_ID
    name             = 'BookToMarketValuePremium'
    description      = 'B/M rank: LONG top quintile (value), SHORT bottom quintile (growth) — Davis, Fama & French (2000)'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 252
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
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Step 1: compute B/M ratio for each ticker ────────────────────────
        bm_scores: dict[str, float] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue

            book_equity = (
                fin.get('totalStockholdersEquity')
                or fin.get('totalEquity')
                or fin.get('bookValue')
                or 0.0
            )
            market_cap = (
                fin.get('marketCap')
                or fin.get('marketCapitalization')
                or 0.0
            )

            # Require positive book equity and market cap
            if book_equity <= 0 or market_cap <= 0:
                continue

            bm = float(book_equity) / float(market_cap)
            bm_scores[ticker] = bm

        if len(bm_scores) < 10:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: cross-sectional percentile rank ──────────────────────────
        tickers = list(bm_scores.keys())
        bm_vals = np.array([bm_scores[t] for t in tickers], dtype=float)
        bm_rank = pd.Series(bm_vals).rank(pct=True).to_numpy()  # high rank = high B/M = value

        # ── Step 3: quintile selection ───────────────────────────────────────
        n = len(tickers)
        q_cut = max(n // 5, 1)
        q_cut = min(q_cut, self.MAX_SIGNALS // 2)

        sorted_idx = np.argsort(bm_vals)
        bottom_idx = sorted_idx[:q_cut]   # SHORT — low B/M (growth)
        top_idx    = sorted_idx[-q_cut:]  # LONG  — high B/M (value)

        per_pos = min(round(scale / max(len(top_idx), 1), 4), 0.05)

        signals: List[Signal] = []

        for idx in top_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            rank = float(bm_rank[idx])
            conf = 'HIGH' if rank > 0.90 else ('MED' if rank > 0.80 else 'LOW')
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
                    'bm_ratio': round(float(bm_vals[idx]), 4),
                    'bm_rank':  round(rank, 4),
                },
            ))

        for idx in bottom_idx:
            t = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            rank = float(bm_rank[idx])
            conf = 'HIGH' if rank < 0.10 else ('MED' if rank < 0.20 else 'LOW')
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
                    'bm_ratio': round(float(bm_vals[idx]), 4),
                    'bm_rank':  round(rank, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
