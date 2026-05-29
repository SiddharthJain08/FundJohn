"""
S_fama_french_three_factor_anomalies — Fama & French (1996) JoF
Composite rank on book-to-market (HML) + size (SMB):
LONG top quintile (high B/M + small cap), SHORT bottom quintile.
Monthly rebalance with 6-month lookahead lag on fiscal year-end data.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import r3000 as universe_filter

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = "S_fama_french_three_factor_anomalies"

__all__ = ['FamaFrenchThreeFactorAnomalies']


class FamaFrenchThreeFactorAnomalies(BaseStrategy):
    """LONG high-B/M small-cap, SHORT low-B/M large-cap — Fama & French (1996) three-factor anomalies."""

    id               = 'S_fama_french_three_factor_anomalies'
    name             = 'FamaFrenchThreeFactorAnomalies'
    description      = ('LONG top-quintile high-B/M small-cap, SHORT bottom-quintile low-B/M large-cap '
                        '— Fama & French (1996) three-factor anomaly capture')
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 1260
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

        financials = (aux_data or {}).get('financials', {}) or {}
        scale = self.position_scale(regime_state)

        # ── Step 1: compute B/M and log-market-cap for each ticker ───────────
        scores: dict = {}
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            ts = prices[ticker].dropna()
            if len(ts) < 63:   # at least 3 months of history
                continue

            price = float(ts.iloc[-1])
            if price <= 0:
                continue

            fin = financials.get(ticker) or {}
            book_eq = (fin.get('totalStockholdersEquity')
                       or fin.get('totalEquity')
                       or fin.get('bookValue') or None)
            mkt_cap = fin.get('marketCap') or fin.get('market_cap') or None
            if mkt_cap is None or mkt_cap <= 0:
                shares = (fin.get('sharesOutstanding')
                          or fin.get('commonStockSharesOutstanding') or None)
                if shares and float(shares) > 0:
                    mkt_cap = float(shares) * price

            if mkt_cap is None or mkt_cap <= 0:
                continue

            btm = None
            if book_eq is not None and float(book_eq) > 0:
                btm = float(book_eq) / float(mkt_cap)

            scores[ticker] = {
                'btm':     btm,
                'log_mc':  float(np.log(float(mkt_cap))),
            }

        if len(scores) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Step 2: cross-sectional z-scores ─────────────────────────────────
        tickers  = list(scores.keys())
        btm_raw  = np.array([scores[t]['btm'] if scores[t]['btm'] is not None else np.nan
                              for t in tickers], dtype=float)
        logmc    = np.array([scores[t]['log_mc'] for t in tickers], dtype=float)

        def _zscore(arr: np.ndarray) -> np.ndarray:
            valid = ~np.isnan(arr)
            if valid.sum() < 5:
                return np.zeros_like(arr)
            mu, sd = arr[valid].mean(), arr[valid].std()
            if sd == 0:
                return np.zeros_like(arr)
            out = np.where(valid, (arr - mu) / sd, 0.0)
            return np.clip(out, -3.0, 3.0)

        z_btm   = _zscore(btm_raw)            # high B/M → HML long leg
        z_size  = _zscore(-logmc)             # small cap (low log-mc) → SMB long leg

        # Composite: equal weight; if B/M missing, use size only (halved weight)
        btm_ok    = ~np.isnan(btm_raw)
        composite = np.where(btm_ok, (z_btm + z_size) / 2.0, z_size * 0.5)

        # ── Step 3: quintile cut ──────────────────────────────────────────────
        p80 = float(np.nanpercentile(composite, 80))
        p20 = float(np.nanpercentile(composite, 20))

        long_idx  = sorted([i for i, s in enumerate(composite) if s >= p80],
                           key=lambda i: composite[i], reverse=True)
        short_idx = sorted([i for i, s in enumerate(composite) if s <= p20],
                           key=lambda i: composite[i])

        max_leg  = min(len(long_idx), len(short_idx), self.MAX_SIGNALS // 2)
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
                    'z_btm':     round(float(z_btm[idx]), 4),
                    'z_size':    round(float(z_size[idx]), 4),
                    'composite': round(c, 4),
                    'btm':       round(float(btm_raw[idx]), 4) if btm_ok[idx] else None,
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
                    'z_btm':     round(float(z_btm[idx]), 4),
                    'z_size':    round(float(z_size[idx]), 4),
                    'composite': round(c, 4),
                    'btm':       round(float(btm_raw[idx]), 4) if btm_ok[idx] else None,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
