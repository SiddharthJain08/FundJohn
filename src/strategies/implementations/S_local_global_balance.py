# src/strategies/implementations/S_local_global_balance.py
from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['LocalGlobalBalance']


class LocalGlobalBalance(BaseStrategy):
    """rank_by(abs(local_balance_node - global_balance_network)); LONG top_decile in HIGH_VOL."""

    id          = 'S_local_global_balance'
    name        = 'LocalGlobalBalance'
    description = 'rank_by(abs(local_balance_node - global_balance_network)); LONG top_decile in HIGH_VOL'
    tier        = 2
    min_lookback = 63
    active_in_regimes = ['HIGH_VOL']

    LOOKBACK  = 63    # rolling correlation window
    TOP_N_PCT = 0.10  # top decile

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        # Filter universe to available columns
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (tickers={len(tickers)} < 10)', file=sys.stderr)
            return []

        prices_sub = prices[tickers].copy()

        # Need at least LOOKBACK rows
        if len(prices_sub) < self.LOOKBACK:
            print(f'[debug] signals=0 (rows={len(prices_sub)} < {self.LOOKBACK})', file=sys.stderr)
            return []

        # Use most recent window; drop tickers with >20% missing
        window = prices_sub.iloc[-self.LOOKBACK:]
        window = window.dropna(axis=1, thresh=int(self.LOOKBACK * 0.8))
        tickers = list(window.columns)
        if len(tickers) < 10:
            print(f'[debug] signals=0 (clean tickers={len(tickers)} < 10)', file=sys.stderr)
            return []

        # Compute returns
        returns = window.pct_change().dropna()
        if len(returns) < 20:
            print(f'[debug] signals=0 (return rows={len(returns)} < 20)', file=sys.stderr)
            return []

        # --- Correlation network ---
        corr = returns.corr()
        # Zero self-loops for clean degree computation
        np.fill_diagonal(corr.values, 0.0)

        # Local balance: signed correlation sum per node (normalized)
        n = len(tickers)
        local_balance = corr.sum(axis=1) / max(n - 1, 1)

        # Global balance: network-wide mean
        global_balance = float(local_balance.mean())

        # Rank score: absolute deviation from global balance
        scores = (local_balance - global_balance).abs().dropna()
        if scores.empty:
            print(f'[debug] signals=0 (empty scores)', file=sys.stderr)
            return []

        # Top decile (most deviant nodes)
        n_long = max(1, int(len(scores) * self.TOP_N_PCT))
        top_tickers = scores.nlargest(n_long).index.tolist()

        scale = self.position_scale(regime_state)
        base_size = min(0.04, 1.0 / max(n_long, 1)) * scale

        latest_prices = window.iloc[-1]
        score_series = scores

        signals: List[Signal] = []
        for ticker in top_tickers:
            if ticker not in latest_prices.index or pd.isna(latest_prices[ticker]):
                continue
            current_price = float(latest_prices[ticker])
            if current_price <= 0:
                continue

            price_series = window[ticker].dropna()
            stops = self.compute_stops_and_targets(
                price_series, 'LONG', current_price, regime_state=regime_state
            )

            score_val = float(score_series.get(ticker, 0.0))
            score_rank = float((score_series < score_val).mean())
            if score_rank >= 0.90:
                confidence = 'HIGH'
            elif score_rank >= 0.70:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=current_price,
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']),
                target_2=float(stops['t2']),
                target_3=float(stops['t3']),
                position_size_pct=float(base_size),
                confidence=confidence,
                signal_params={
                    'local_balance':    round(float(local_balance.get(ticker, 0.0)), 6),
                    'global_balance':   round(global_balance, 6),
                    'balance_deviation': round(score_val, 6),
                    'lookback':         self.LOOKBACK,
                },
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
