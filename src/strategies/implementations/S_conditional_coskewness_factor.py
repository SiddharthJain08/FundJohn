"""
Conditional Coskewness Factor — Harvey & Siddique (2000)

Stocks with negative systematic coskewness demand a 3.60%/yr risk premium.
Long bottom 30% (most negative coskewness), short top 30% (most positive).
Rebalances monthly using rolling 60-month coskewness estimates.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['ConditionalCoskewnessFactor']


class ConditionalCoskewnessFactor(BaseStrategy):
    """Long stocks with most negative coskewness vs market; short most positive. Monthly rebalance."""

    id          = 'S_conditional_coskewness_factor'
    name        = 'ConditionalCoskewnessFactor'
    description = 'Long-short coskewness factor: LONG neg-skew stocks, SHORT pos-skew stocks, monthly rebalance'
    tier        = 2
    min_lookback = 756  # 60 months × ~12.6 trading days/month

    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    # Rolling window for coskewness estimation (60 months ≈ 1260 trading days)
    COSKEW_WINDOW = 1260
    # Minimum observations needed for a reliable regression
    MIN_OBS = 252
    # Fraction of universe to go long/short
    QUANTILE = 0.30
    # Max signals per side
    MAX_SIDE = 15

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

        scale = self.position_scale(regime_state)

        # Need enough history
        if len(prices) < self.MIN_OBS:
            print(f'[debug] signals=0 (insufficient history: {len(prices)} rows)', file=sys.stderr)
            return []

        # Use available tickers in universe that are in prices
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        # Compute daily returns
        price_data = prices[tickers].copy()
        returns = price_data.pct_change().dropna(how='all')

        if len(returns) < self.MIN_OBS:
            print(f'[debug] signals=0 (insufficient return rows: {len(returns)})', file=sys.stderr)
            return []

        # Use SPY or first ticker as market proxy
        market_col = 'SPY' if 'SPY' in returns.columns else returns.columns[0]
        window = min(self.COSKEW_WINDOW, len(returns))

        ret_window = returns.iloc[-window:]
        mkt_ret = ret_window[market_col].values

        # Standardize market returns for regression
        mkt_mean = mkt_ret.mean()
        mkt_std = mkt_ret.std()
        if mkt_std < 1e-10:
            print(f'[debug] signals=0 (degenerate market returns)', file=sys.stderr)
            return []

        mkt_z = (mkt_ret - mkt_mean) / mkt_std
        mkt_z2 = mkt_z ** 2  # squared term for coskewness

        # Design matrix: [1, r_m, r_m^2]
        X = np.column_stack([np.ones(len(mkt_z)), mkt_z, mkt_z2])

        coskew_scores = {}
        for ticker in tickers:
            if ticker == market_col:
                continue
            y = ret_window[ticker].values
            valid = ~np.isnan(y)
            if valid.sum() < self.MIN_OBS:
                continue
            y_v = y[valid]
            X_v = X[valid]
            try:
                # OLS via normal equations
                XtX = X_v.T @ X_v
                Xty = X_v.T @ y_v
                coeffs = np.linalg.lstsq(XtX, Xty, rcond=None)[0]
                # Standardize coefficient on r_m^2
                y_std = y_v.std()
                if y_std > 1e-10:
                    coskew_scores[ticker] = float(coeffs[2] / y_std)
            except Exception:
                continue

        if len(coskew_scores) < 20:
            print(f'[debug] signals=0 (too few coskew estimates: {len(coskew_scores)})', file=sys.stderr)
            return []

        scores_series = pd.Series(coskew_scores)
        n = len(scores_series)
        n_side = max(1, int(n * self.QUANTILE))

        longs = scores_series.nsmallest(n_side).index.tolist()[:self.MAX_SIDE]
        shorts = scores_series.nlargest(n_side).index.tolist()[:self.MAX_SIDE]

        # Per-signal sizing: split budget across positions
        n_total = len(longs) + len(shorts)
        base_size = min(0.04, scale * 0.60 / max(n_total, 1))

        signals: List[Signal] = []
        latest_prices = price_data.iloc[-1]

        for ticker in longs:
            if ticker not in latest_prices.index or pd.isna(latest_prices[ticker]):
                continue
            price = float(latest_prices[ticker])
            if price <= 0:
                continue
            stops = self.compute_stops_and_targets(
                price_data[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(base_size, 4),
                confidence='MED',
                signal_params={
                    'coskewness_score': round(coskew_scores[ticker], 6),
                    'coskewness_rank': 'bottom_30pct',
                    'lookback_days': window,
                },
            ))

        for ticker in shorts:
            if ticker not in latest_prices.index or pd.isna(latest_prices[ticker]):
                continue
            price = float(latest_prices[ticker])
            if price <= 0:
                continue
            stops = self.compute_stops_and_targets(
                price_data[ticker].dropna(), 'SHORT', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(base_size, 4),
                confidence='MED',
                signal_params={
                    'coskewness_score': round(coskew_scores[ticker], 6),
                    'coskewness_rank': 'top_30pct',
                    'lookback_days': window,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
