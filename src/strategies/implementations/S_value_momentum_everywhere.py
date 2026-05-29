"""
S_value_momentum_everywhere — Asness, Moskowitz & Pedersen (2013) JoF
Value (book-to-market) + momentum (12-1m return) composite long-short.
LONG top-tercile, SHORT bottom-tercile; equal-weight; monthly rebalance.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import sp500 as universe_filter

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = "S_value_momentum_everywhere"

__all__ = ['ValueMomentumEverywhere']


class ValueMomentumEverywhere(BaseStrategy):
    """Value (B/M) + momentum (12-1m) composite: LONG top-tercile, SHORT bottom-tercile."""

    id               = 'S_value_momentum_everywhere'
    name             = 'ValueMomentumEverywhere'
    description      = ('Value (book-to-market) and momentum (12-1m return) composite: '
                        'LONG top-tercile, SHORT bottom-tercile — Asness, Moskowitz & Pedersen (2013)')
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 1260
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    # Momentum window parameters
    MOM_LOOKBACK = 252   # ~12 months in trading days
    MOM_SKIP     = 21    # ~1 month skip (avoid short-term reversal)

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

        financials = (aux_data or {}).get('financials', {}) or {}
        scale = self.position_scale(regime_state)

        # ── Step 1: compute value (B/M) and momentum (12-1m) for each ticker ─
        scores: dict = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            ts = prices[ticker].dropna()
            if len(ts) < self.MOM_LOOKBACK + self.MOM_SKIP + 5:
                continue

            # Momentum: 12-1m return (skip most recent month)
            ret_12_1 = float(
                ts.iloc[-(self.MOM_SKIP + 1)] / ts.iloc[-(self.MOM_LOOKBACK + self.MOM_SKIP)] - 1
            )

            # Value: book-to-market
            btm = None
            fin = financials.get(ticker)
            if fin:
                book_eq = (fin.get('totalStockholdersEquity')
                           or fin.get('totalEquity')
                           or fin.get('bookValue') or 0.0)
                mkt_cap = fin.get('marketCap') or fin.get('market_cap') or 0.0
                if mkt_cap <= 0:
                    shares = (fin.get('sharesOutstanding')
                              or fin.get('commonStockSharesOutstanding') or 0.0)
                    if shares:
                        mkt_cap = float(shares) * float(ts.iloc[-1])
                if book_eq > 0 and mkt_cap > 0:
                    btm = float(book_eq) / float(mkt_cap)

            scores[ticker] = {'ret_12_1': ret_12_1, 'btm': btm}

        if len(scores) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: cross-sectional z-scores ─────────────────────────────────
        tickers   = list(scores.keys())
        mom_vals  = np.array([scores[t]['ret_12_1'] for t in tickers], dtype=float)
        btm_raw   = np.array([scores[t]['btm'] if scores[t]['btm'] is not None else np.nan
                               for t in tickers], dtype=float)

        def _zscore(arr: np.ndarray) -> np.ndarray:
            valid = ~np.isnan(arr)
            if valid.sum() < 5:
                return np.zeros_like(arr)
            mu, sd = arr[valid].mean(), arr[valid].std()
            if sd == 0:
                return np.zeros_like(arr)
            return np.clip(np.where(valid, (arr - mu) / sd, 0.0), -3.0, 3.0)

        z_mom = _zscore(mom_vals)
        z_val = _zscore(btm_raw)

        # Composite: equal-weight; halve momentum weight when value signal absent
        btm_ok    = ~np.isnan(btm_raw)
        composite = np.where(btm_ok, (z_mom + z_val) / 2.0, z_mom * 0.5)

        # ── Step 3: tercile cut ───────────────────────────────────────────────
        p33 = float(np.nanpercentile(composite, 33))
        p67 = float(np.nanpercentile(composite, 67))

        long_idx  = sorted([i for i, s in enumerate(composite) if s >= p67],
                           key=lambda i: composite[i], reverse=True)
        short_idx = sorted([i for i, s in enumerate(composite) if s <= p33],
                           key=lambda i: composite[i])

        max_leg   = min(len(long_idx), len(short_idx), self.MAX_SIGNALS // 2)
        long_idx  = long_idx[:max_leg]
        short_idx = short_idx[:max_leg]

        if max_leg == 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        per_pos = min(round(scale / max_leg, 4), 0.04)

        signals: List[Signal] = []

        for idx in long_idx:
            t  = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            c = float(composite[idx])
            signals.append(Signal(
                ticker            = t,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_pos,
                confidence        = 'HIGH' if c > 1.0 else ('MED' if c > 0.5 else 'LOW'),
                signal_params     = {
                    'z_mom':     round(float(z_mom[idx]), 4),
                    'z_val':     round(float(z_val[idx]) if btm_ok[idx] else 0.0, 4),
                    'composite': round(c, 4),
                    'ret_12_1':  round(float(mom_vals[idx]), 4),
                },
            ))

        for idx in short_idx:
            t  = tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            c = float(composite[idx])
            signals.append(Signal(
                ticker            = t,
                direction         = 'SHORT',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = per_pos,
                confidence        = 'HIGH' if c < -1.0 else ('MED' if c < -0.5 else 'LOW'),
                signal_params     = {
                    'z_mom':     round(float(z_mom[idx]), 4),
                    'z_val':     round(float(z_val[idx]) if btm_ok[idx] else 0.0, 4),
                    'composite': round(c, 4),
                    'ret_12_1':  round(float(mom_vals[idx]), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
