"""
S_january_btm_size_seasonal — Loughran (1997)
January seasonal B/M long/short: enter December, exit February.
LONG high-B/M small-cap, SHORT low-B/M small-cap (exclude top market-cap quintile).
Source: https://doi.org/10.2307/2331199
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['JanuaryBTMSizeSeasonal']


class JanuaryBTMSizeSeasonal(BaseStrategy):
    """January BTM seasonal: LONG high-B/M, SHORT low-B/M small-cap in December; FLAT in February."""

    id               = 'S_january_btm_size_seasonal'
    name             = 'JanuaryBTMSizeSeasonal'
    description      = ('January seasonal B/M effect concentrated in small-cap stocks: '
                        'LONG high-B/M small-cap, SHORT low-B/M small-cap each December; exit February.')
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

        try:
            current_date = pd.to_datetime(prices.index[-1])
        except Exception:
            print('[debug] signals=0', file=sys.stderr)
            return []

        month = current_date.month
        # Only act in December (entry) or February (exit)
        if month not in (12, 2):
            print('[debug] signals=0', file=sys.stderr)
            return []

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # Compute BTM and market cap for each ticker in universe
        btm_scores: dict[str, float] = {}
        mkt_caps: dict[str, float] = {}

        for ticker in universe:
            if ticker not in prices.columns:
                continue
            ts = prices[ticker].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            if price <= 0:
                continue

            fin = financials.get(ticker)
            if not fin:
                continue

            # Book value per share
            bvps = float(fin.get('bookValuePerShare') or 0.0)
            if bvps <= 0:
                equity = float(fin.get('totalStockholdersEquity') or fin.get('totalEquity') or 0.0)
                shares_raw = float(fin.get('commonStockSharesOutstanding') or fin.get('sharesOutstanding') or 0.0)
                if equity > 0 and shares_raw > 0:
                    bvps = equity / shares_raw
            if bvps <= 0:
                continue

            btm = bvps / price
            if not np.isfinite(btm) or btm <= 0:
                continue

            shares_raw = float(fin.get('commonStockSharesOutstanding') or fin.get('sharesOutstanding') or 0.0)
            mkt_cap = shares_raw * price if shares_raw > 0 else price * 1e6

            btm_scores[ticker] = btm
            mkt_caps[ticker] = mkt_cap

        if len(btm_scores) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        tickers = list(btm_scores.keys())
        caps = np.array([mkt_caps[t] for t in tickers], dtype=float)
        btms = np.array([btm_scores[t] for t in tickers], dtype=float)

        # Exclude top market-cap quintile (largest 20% ~= 73% of total market cap)
        cap_thresh = np.percentile(caps, 80)
        small_mask = caps <= cap_thresh
        small_tickers = [t for t, m in zip(tickers, small_mask) if m]
        small_btms = btms[small_mask]

        if len(small_tickers) < 10:
            print('[debug] signals=0', file=sys.stderr)
            return []

        n = len(small_tickers)
        q_cut = max(n // 5, 1)
        q_cut = min(q_cut, self.MAX_SIGNALS // 2)

        sorted_idx = np.argsort(small_btms)
        short_idx = sorted_idx[:q_cut]    # low B/M = growth → SHORT
        long_idx  = sorted_idx[-q_cut:]   # high B/M = value  → LONG

        signals: List[Signal] = []

        if month == 2:
            # February: exit all January seasonal positions
            for idx in list(long_idx) + list(short_idx):
                t = small_tickers[idx]
                ts = prices[t].dropna()
                if len(ts) < 5:
                    continue
                price = float(ts.iloc[-1])
                signals.append(Signal(
                    ticker            = t,
                    direction         = 'FLAT',
                    entry_price       = price,
                    stop_loss         = round(price * 0.98, 4),
                    target_1          = price,
                    target_2          = price,
                    target_3          = price,
                    position_size_pct = 0.0,
                    confidence        = 'LOW',
                    signal_params     = {'reason': 'january_seasonal_exit',
                                         'btm': round(float(small_btms[idx]), 4)},
                ))
            print(f'[debug] signals={len(signals)}', file=sys.stderr)
            return signals[:self.MAX_SIGNALS]

        # December: enter January seasonal positions
        per_pos = min(round(scale / max(q_cut, 1), 4), 0.04)

        for idx in long_idx:
            t = small_tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            btm_v = float(small_btms[idx])
            conf  = 'HIGH' if btm_v > 2.0 else ('MED' if btm_v > 1.0 else 'LOW')
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
                signal_params     = {'btm': round(btm_v, 4), 'quintile': 5, 'size_filter': 'small4'},
            ))

        for idx in short_idx:
            t = small_tickers[idx]
            ts = prices[t].dropna()
            if len(ts) < 20:
                continue
            price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            btm_v = float(small_btms[idx])
            conf  = 'HIGH' if btm_v < 0.3 else ('MED' if btm_v < 0.6 else 'LOW')
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
                signal_params     = {'btm': round(btm_v, 4), 'quintile': 1, 'size_filter': 'small4'},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
