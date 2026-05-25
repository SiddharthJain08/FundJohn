"""
S_fama_french_three_factor_composite — Fama & French (1996, JF)
Composite SMB+HML rank: LONG top-tercile (small + high BM), SHORT bottom-tercile.
composite = 0.5 * zscore(-log(mkt_cap)) + 0.5 * zscore(book_equity / mkt_cap)
Monthly rebalance; financials lagged 6 months to avoid look-ahead bias.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['FamaFrenchThreeFactorComposite']


class FamaFrenchThreeFactorComposite(BaseStrategy):
    """Long small-cap/high-BM, short large-cap/low-BM composite rank factor."""

    id               = 'S_fama_french_three_factor_composite'
    name             = 'FamaFrenchThreeFactorComposite'
    description      = ('Small-cap and high book-to-market stocks earn persistent excess returns; '
                        'composite SMB+HML z-score rank: LONG top tercile, SHORT bottom tercile.')
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 1260
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

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Step 1: compute size + BM scores per ticker ───────────────────────
        composite_scores: dict[str, float] = {}
        raw_bm: dict[str, float] = {}
        raw_size: dict[str, float] = {}

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
            if price <= 0:
                continue

            # Market cap: shares_outstanding × price
            shares = float(
                fin.get('commonStockSharesOutstanding')
                or fin.get('sharesOutstanding')
                or 0.0
            )
            if shares <= 0:
                continue
            mkt_cap = shares * price

            # Book equity (6-month lagged via financials quarterly data)
            book_eq = float(
                fin.get('totalStockholdersEquity')
                or fin.get('totalEquity')
                or fin.get('bookValuePerShare', 0.0) * shares
                or 0.0
            )
            if book_eq <= 0:
                continue

            bm = book_eq / mkt_cap
            raw_size[ticker] = float(np.log(mkt_cap))
            raw_bm[ticker] = float(bm)

        if len(raw_size) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: cross-sectional z-scores ─────────────────────────────────
        tickers = list(raw_size.keys())
        size_arr = np.array([raw_size[t] for t in tickers], dtype=float)
        bm_arr   = np.array([raw_bm[t]   for t in tickers], dtype=float)

        def zscore(arr: np.ndarray) -> np.ndarray:
            std = arr.std()
            if std == 0:
                return np.zeros_like(arr)
            return (arr - arr.mean()) / std

        z_size = zscore(-size_arr)   # negate: small cap → higher score
        z_bm   = zscore(bm_arr)      # high BM → higher score
        composite = 0.5 * z_size + 0.5 * z_bm

        # ── Step 3: tercile selection ─────────────────────────────────────────
        n       = len(tickers)
        t_cut   = max(n // 3, 1)
        t_cut   = min(t_cut, self.MAX_SIGNALS // 2)

        sorted_idx = np.argsort(composite)
        short_idx  = sorted_idx[:t_cut]          # lowest composite → SHORT
        long_idx   = sorted_idx[-t_cut:][::-1]   # highest composite → LONG

        per_pos = min(round(scale / max(t_cut, 1), 4), 0.04)

        signals: List[Signal] = []

        for idx in long_idx:
            t   = tickers[idx]
            ts  = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            comp_v = float(composite[idx])
            conf   = 'HIGH' if comp_v > 1.5 else ('MED' if comp_v > 0.5 else 'LOW')
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
                    'composite_score': round(comp_v, 4),
                    'z_size': round(float(z_size[idx]), 4),
                    'z_bm':   round(float(z_bm[idx]), 4),
                    'tercile': 'top',
                },
            ))

        for idx in short_idx:
            t   = tickers[idx]
            ts  = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            comp_v = float(composite[idx])
            conf   = 'HIGH' if comp_v < -1.5 else ('MED' if comp_v < -0.5 else 'LOW')
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
                    'composite_score': round(comp_v, 4),
                    'z_size': round(float(z_size[idx]), 4),
                    'z_bm':   round(float(z_bm[idx]), 4),
                    'tercile': 'bottom',
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
