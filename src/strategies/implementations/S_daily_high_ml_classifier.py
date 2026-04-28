"""
S_daily_high_ml_classifier — Daily high ML classifier strategy.

Paper: Novak & Velušček (2015) — "Prediction of stock price movement based
on daily high prices." Quantitative Finance, 2015.

Hypothesis: Daily high prices exhibit lower intraday noise than closing prices;
statistical classifiers on lagged OHLCV features can predict next-day high
direction, and going long predicted-UP stocks outperforms the S&P 500.
"""
from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['DailyHighMLClassifier']

# Number of lag periods for feature construction
_LOOKBACK = 20
# Classifier: rolling logistic regression proxy via sign-vote on lagged features
_TOP_N = 20  # max long signals per day


class DailyHighMLClassifier(BaseStrategy):
    """Long stocks with predicted upward next-day high via rolling LDA-proxy classifier."""

    id          = 'S_daily_high_ml_classifier'
    name        = 'DailyHighMLClassifier'
    description = (
        'Predict next-day high direction using lagged OHLCV features; '
        'go long equal-weight portfolio of predicted-UP stocks daily.'
    )
    tier        = 2
    min_lookback = 504  # ~2 trading years
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # Require multi-level columns: (field, ticker)
        if not isinstance(prices.columns, pd.MultiIndex):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Extract OHLCV columns available
        fields = prices.columns.get_level_values(0).unique().tolist()
        required = {'open', 'high', 'low', 'close', 'volume'}
        if not required.issubset(set(f.lower() for f in fields)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Normalize column level names to lowercase
        prices.columns = pd.MultiIndex.from_tuples(
            [(c[0].lower(), c[1]) for c in prices.columns]
        )

        # Filter to universe tickers that exist in prices
        available_tickers = prices.columns.get_level_values(1).unique().tolist()
        tickers = [t for t in universe if t in available_tickers]
        if len(tickers) < 10:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Need at least lookback rows
        if len(prices) < _LOOKBACK + 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        signals: List[Signal] = []

        for ticker in tickers:
            try:
                hi  = prices['high'][ticker].dropna()
                cl  = prices['close'][ticker].dropna()
                op  = prices['open'][ticker].dropna()
                lo  = prices['low'][ticker].dropna()
                vol = prices['volume'][ticker].dropna()

                # Align all series
                idx = hi.index.intersection(cl.index).intersection(op.index).intersection(lo.index).intersection(vol.index)
                if len(idx) < _LOOKBACK + 2:
                    continue

                hi  = hi.loc[idx]
                cl  = cl.loc[idx]
                op  = op.loc[idx]
                lo  = lo.loc[idx]
                vol = vol.loc[idx]

                # Build label: sign(high[t+1] - high[t])  (1=UP, -1=DOWN)
                label = np.sign(hi.diff().shift(-1))

                # Build features: lagged ratios (Novak & Velušček §3)
                feat_df = pd.DataFrame(index=idx)
                for lag in range(1, min(6, _LOOKBACK + 1)):
                    feat_df[f'hi_ret_{lag}']   = hi.diff(lag) / hi.shift(lag)
                    feat_df[f'cl_ret_{lag}']   = cl.diff(lag) / cl.shift(lag)
                    feat_df[f'hl_spread_{lag}'] = (hi.shift(lag) - lo.shift(lag)) / cl.shift(lag)

                feat_df['vol_chg'] = np.log(vol / vol.shift(1) + 1e-9)
                feat_df['open_gap'] = (op - cl.shift(1)) / cl.shift(1)

                feat_df = feat_df.replace([np.inf, -np.inf], np.nan).dropna()
                label_aligned = label.reindex(feat_df.index).dropna()
                feat_df = feat_df.loc[label_aligned.index]

                if len(feat_df) < _LOOKBACK:
                    continue

                # Rolling LDA-proxy: weighted sign vote on lagged return features
                # For each feature, compute correlation with label on training window,
                # then predict via sign(weighted_sum). Avoids scipy covariance issues.
                train = feat_df.iloc[:-1]
                train_labels = label_aligned.iloc[:-1]
                test_row = feat_df.iloc[-1]

                if len(train) < _LOOKBACK:
                    continue

                # Weight each feature by its rolling Spearman rank correlation with label
                weights = []
                for col in feat_df.columns:
                    corr = train[col].tail(_LOOKBACK).corr(train_labels.tail(_LOOKBACK))
                    weights.append(corr if not np.isnan(corr) else 0.0)

                weights = np.array(weights)
                score = float(np.dot(weights, test_row.values))
                predicted_up = score > 0

                if not predicted_up:
                    continue

                # Entry: last close price
                current_price = float(cl.iloc[-1])
                if current_price <= 0:
                    continue

                prices_series = cl.iloc[-30:] if len(cl) >= 30 else cl
                st = self.compute_stops_and_targets(
                    prices_series=prices_series,
                    direction='LONG',
                    current_price=current_price,
                    regime_state=regime_state,
                )

                # Confidence: map |score| magnitude to tier
                abs_score = abs(score)
                if abs_score > 0.5:
                    confidence = 'HIGH'
                elif abs_score > 0.2:
                    confidence = 'MED'
                else:
                    confidence = 'LOW'

                base_size = 0.02  # 2% per position (50-stock universe cap)
                position_size_pct = float(round(base_size * scale, 4))

                signals.append(Signal(
                    ticker=ticker,
                    direction='LONG',
                    entry_price=float(round(current_price, 4)),
                    stop_loss=float(round(st['stop'], 4)),
                    target_1=float(round(st['t1'], 4)),
                    target_2=float(round(st['t2'], 4)),
                    target_3=float(round(st['t3'], 4)),
                    position_size_pct=position_size_pct,
                    confidence=confidence,
                    signal_params={
                        'score': float(round(score, 4)),
                        'regime': regime_state,
                        'lookback': _LOOKBACK,
                    },
                ))
            except Exception:
                continue

        # Cap and sort by score descending
        signals.sort(key=lambda s: abs(s.signal_params.get('score', 0)), reverse=True)
        signals = signals[:min(_TOP_N, self.MAX_SIGNALS)]

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
