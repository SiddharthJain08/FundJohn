"""
S_ast_value_book_to_market_factor — HML Book-to-Market Value Factor (Fama & French / Quantpedia)

Long bottom-quintile P/B stocks (cheapest, highest B/M = value) and short top-quintile P/B
stocks (most expensive, lowest B/M = growth). Equal-weight within each leg. Annual rebalance
in December, matching the original HML paper's fiscal-year-end B/M alignment.

Source: https://quantpedia.com/strategies/value-book-to-market-factor/
Reference: Fama & French (1992, 1993) — the HML factor.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstValueBookToMarketFactor']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_ast_value_book_to_market_factor'


class AstValueBookToMarketFactor(BaseStrategy):
    """Long low-P/B (value) / short high-P/B (growth) — HML factor, annual December rebalance."""

    id               = STRATEGY_ID
    name             = 'AstValueBookToMarketFactor'
    description      = ('Stocks with low P/B (high B/M = value) outperform high P/B (growth) '
                        'stocks; long bottom quintile, short top quintile, annual December rebalance.')
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 252
    # Value premium is an all-weather academic factor; long-short hedges market exposure
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

        # Annual December rebalance: fire on the FIRST bar inside December —
        # once per year under daily AND monthly sampling.
        try:
            last_date = pd.Timestamp(prices.index[-1])
            prev_date = (pd.Timestamp(prices.index[-2])
                         if len(prices.index) >= 2 else None)
            into_dec = last_date.month == 12 and (
                prev_date is None or prev_date.month != 12)
        except Exception:
            into_dec = True  # fail-open: always run if dates unparseable

        if not into_dec:
            print('[debug] signals=0 (not December-entry bar)', file=sys.stderr)
            return []

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            # Distinguish the three early-return causes (Sunday review) so a
            # zero-trade backtest is self-explanatory.
            print('[debug] signals=0 (aux financials empty)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Compute B/M (book-to-market) for each ticker ────────────────────
        bm_data: dict[str, dict] = {}
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

            # Book equity: the financials panel has no stockholders'-equity
            # field, but totalAssets − totalLiabilities IS book equity —
            # derive it (2026-08-10, Sunday review: the old key lookups
            # never matched anything and every ticker was skipped). Keep the
            # legacy keys as a first preference should a richer feed appear.
            book_eq = float(
                fin.get('totalStockholdersEquity')
                or fin.get('totalEquity')
                or fin.get('bookValue')
                or 0.0
            )
            if book_eq <= 0:
                ta = fin.get('totalAssets')
                tl = fin.get('totalLiabilities')
                if ta is not None and tl is not None:
                    book_eq = float(ta) - float(tl)
            if book_eq <= 0:
                continue

            # Market cap: shares * price, or the panel's marketCap field
            shares = float(fin.get('sharesOutstanding') or fin.get('commonStockSharesOutstanding') or 0.0)
            mktcap = shares * price if shares > 0 else float(fin.get('marketCap') or 0.0)
            if mktcap <= 0:
                continue

            pb = mktcap / book_eq   # P/B ratio: low = cheap (value)
            bm = book_eq / mktcap   # B/M ratio: high = cheap (value)

            bm_data[ticker] = {
                'pb': pb, 'bm': bm,
                'price': price, 'ts': ts, 'mktcap': mktcap,
            }

        if len(bm_data) < 10:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Quintile sort on P/B ascending ──────────────────────────────────
        tickers = list(bm_data.keys())
        pb_arr  = np.array([bm_data[t]['pb'] for t in tickers], dtype=float)

        n        = len(tickers)
        quintile = max(n // 5, 1)
        quintile = min(quintile, self.MAX_SIGNALS // 2)   # cap at 25 per leg

        sorted_idx = np.argsort(pb_arr)          # ascending: lowest P/B first
        long_idx   = sorted_idx[:quintile]        # bottom quintile → LONG  (cheapest / value)
        short_idx  = sorted_idx[-quintile:]       # top quintile   → SHORT (most expensive / growth)

        per_long  = min(round(scale / max(len(long_idx),  1), 4), 0.05)
        per_short = min(round(scale / max(len(short_idx), 1), 4), 0.05)

        signals: List[Signal] = []

        for idx in long_idx:
            t = tickers[idx]
            d = bm_data[t]
            stops = self.compute_stops_and_targets(d['ts'], 'LONG', d['price'], regime_state=regime_state)
            bm = d['bm']
            conf = 'HIGH' if bm > 2.0 else ('MED' if bm > 1.0 else 'LOW')
            signals.append(Signal(
                ticker            = t,
                direction         = 'LONG',
                entry_price       = float(d['price']),
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_long,
                confidence        = conf,
                signal_params     = {
                    'pb_ratio': round(float(d['pb']), 4),
                    'bm_ratio': round(float(bm), 4),
                    'mktcap':   round(float(d['mktcap']), 0),
                    'leg':      'value_long',
                },
            ))

        for idx in short_idx:
            t = tickers[idx]
            d = bm_data[t]
            stops = self.compute_stops_and_targets(d['ts'], 'SHORT', d['price'], regime_state=regime_state)
            bm = d['bm']
            conf = 'HIGH' if bm < 0.3 else ('MED' if bm < 0.5 else 'LOW')
            signals.append(Signal(
                ticker            = t,
                direction         = 'SHORT',
                entry_price       = float(d['price']),
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_short,
                confidence        = conf,
                signal_params     = {
                    'pb_ratio': round(float(d['pb']), 4),
                    'bm_ratio': round(float(bm), 4),
                    'mktcap':   round(float(d['mktcap']), 0),
                    'leg':      'growth_short',
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
