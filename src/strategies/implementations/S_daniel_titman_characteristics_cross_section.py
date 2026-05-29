"""
S_daniel_titman_characteristics_cross_section — Daniel & Titman (JF 1997)
Small-cap + high book-to-market characteristics long-short portfolio.
LONG top quintile (small + high B/M), SHORT bottom quintile (large + low B/M).
Monthly rebalance using latest market cap and book equity from financials.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import r3000 as universe_filter

__all__ = ['DanielTitmanCharacteristicsCrossSection']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_daniel_titman_characteristics_cross_section'


class DanielTitmanCharacteristicsCrossSection(BaseStrategy):
    """LONG small-cap/high-B/M, SHORT large-cap/low-B/M — Daniel & Titman (1997)."""

    id               = STRATEGY_ID
    name             = 'DanielTitmanCharacteristicsCrossSection'
    description      = 'LONG small-cap/high-B/M, SHORT large-cap/low-B/M — Daniel & Titman (1997)'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 252
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
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Step 1: compute market_cap and B/M for each ticker ───────────────
        scores: dict[str, dict] = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue

            # Market cap: shares * price (or pre-computed field)
            shares = (fin.get('sharesOutstanding')
                      or fin.get('commonStockSharesOutstanding') or 0.0)
            ts = prices[ticker].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            mktcap = float(shares) * price if shares else (fin.get('marketCap') or 0.0)

            if mktcap <= 0:
                # Try direct marketCap field as fallback
                mktcap = float(fin.get('marketCap') or 0.0)
            if mktcap <= 0:
                continue

            # Book equity (total stockholders' equity)
            book_eq = (fin.get('totalStockholdersEquity')
                       or fin.get('totalEquity')
                       or fin.get('bookValue') or 0.0)
            if float(book_eq) <= 0:
                continue

            bm = float(book_eq) / float(mktcap)
            scores[ticker] = {'mktcap': float(mktcap), 'bm': bm, 'price': price, 'ts': ts}

        if len(scores) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: cross-sectional percentile ranks ─────────────────────────
        tickers = list(scores.keys())
        mktcaps = np.array([scores[t]['mktcap'] for t in tickers], dtype=float)
        bms     = np.array([scores[t]['bm']     for t in tickers], dtype=float)

        # small-cap = high rank (negate mktcap so small gets high percentile)
        size_rank = pd.Series(-mktcaps).rank(pct=True).to_numpy()
        bm_rank   = pd.Series(bms).rank(pct=True).to_numpy()

        composite = 0.5 * size_rank + 0.5 * bm_rank   # range [0, 1]

        # ── Step 3: quintile selection ───────────────────────────────────────
        n       = len(tickers)
        q_cut   = max(n // 5, 1)
        q_cut   = min(q_cut, self.MAX_SIGNALS // 2)
        sorted_idx  = np.argsort(composite)
        bottom_idx  = sorted_idx[:q_cut]    # SHORT — large cap + low B/M
        top_idx     = sorted_idx[-q_cut:]   # LONG  — small cap + high B/M

        per_pos = min(round(scale / max(len(top_idx), 1), 4), 0.05)

        signals: List[Signal] = []

        for idx in top_idx:
            t    = tickers[idx]
            ts   = scores[t]['ts']
            price = scores[t]['price']
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            c    = float(composite[idx])
            conf = 'HIGH' if c > 0.85 else ('MED' if c > 0.75 else 'LOW')
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
                    'size_rank': round(float(size_rank[idx]), 4),
                    'bm_rank':   round(float(bm_rank[idx]),   4),
                    'composite': round(c, 4),
                    'bm_ratio':  round(scores[t]['bm'], 4),
                },
            ))

        for idx in bottom_idx:
            t    = tickers[idx]
            ts   = scores[t]['ts']
            price = scores[t]['price']
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            c    = float(composite[idx])
            conf = 'HIGH' if c < 0.15 else ('MED' if c < 0.25 else 'LOW')
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
                    'size_rank': round(float(size_rank[idx]), 4),
                    'bm_rank':   round(float(bm_rank[idx]),   4),
                    'composite': round(c, 4),
                    'bm_ratio':  round(scores[t]['bm'], 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
