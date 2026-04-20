"""
S_alpha191_lasso_crossmarket — Alpha191-inspired factor matrix + LASSO-proxy selection
→ LONG top-decile, SHORT bottom-decile on SP500 universe (>100 names required).

Note: Alpha191 originated for Chinese A-shares; here we use the price/return factor
sub-library that transfers structurally to US equities. LASSO variable selection is
approximated via IC-weighted factor ranking (avoids covariance singularity risk at
large universes).
"""
from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['Alpha191LassoCrossMarket']


class Alpha191LassoCrossMarket(BaseStrategy):
    """Alpha191 factor matrix + LASSO selection → LONG top-decile SHORT bottom-decile."""

    id          = 'S_alpha191_lasso_crossmarket'
    name        = 'Alpha191LassoCrossMarket'
    description = 'Alpha191 factor matrix + LASSO selection → LONG top-decile SHORT bottom-decile'
    tier        = 2
    min_lookback = 252

    _LOOKBACKS = [5, 10, 21, 63, 126, 252]

    def _compute_factors(self, prices: pd.DataFrame, universe: List[str]) -> pd.DataFrame:
        """Compute Alpha191-inspired price factors. Returns ticker × factor matrix."""
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            return pd.DataFrame()

        px = prices[tickers].dropna(how='all').tail(270)
        if len(px) < 63:
            return pd.DataFrame()

        ret = px.pct_change()
        features: dict = {}

        for lb in self._LOOKBACKS:
            if lb < len(px):
                features[f'mom_{lb}']      = px.iloc[-1] / px.iloc[-lb] - 1
                features[f'vol_{lb}']      = ret.tail(lb).std()
                features[f'ret_mean_{lb}'] = ret.tail(lb).mean()

        # Skewness / kurtosis over 63-day window
        features['skew_63'] = ret.tail(63).skew()
        features['kurt_63'] = ret.tail(63).kurt()

        # Short-term reversal: 5d return minus 21d return
        if len(px) > 21:
            r5  = px.iloc[-1] / px.iloc[-5]  - 1
            r21 = px.iloc[-1] / px.iloc[-21] - 1
            features['reversal_5_21'] = r5 - r21

        # 52-week high proximity
        high_52w = px.tail(252).max()
        features['high52w_prox'] = px.iloc[-1] / high_52w.replace(0, np.nan)

        # Max drawdown over 63d (negative signal for LONG candidates)
        rolling_max = px.tail(63).cummax()
        drawdown = (px.tail(63) / rolling_max - 1).min()
        features['dd_63'] = drawdown

        factor_df = pd.DataFrame(features, index=tickers)
        return factor_df.dropna(how='any')

    def _lasso_select_score(self, factor_df: pd.DataFrame) -> pd.Series:
        """
        IC-weighted factor composite — LASSO-proxy via sparsity threshold.
        Weights each factor by its absolute cross-sectional IC vs 21d momentum,
        then zeros out weights below the median (mimicking LASSO sparsity).
        """
        if factor_df.empty or len(factor_df) < 20:
            return pd.Series(dtype=float)

        ranks = factor_df.rank(pct=True)

        if 'mom_21' not in ranks.columns:
            return ranks.mean(axis=1)

        target = ranks['mom_21']
        ic = ranks.corrwith(target).abs().fillna(0)

        # Sparsity: zero out bottom-half IC factors (LASSO-like)
        threshold = ic.quantile(0.5)
        ic[ic < threshold] = 0.0

        ic_sum = ic.sum()
        if ic_sum < 1e-9:
            return ranks.mean(axis=1)

        weights = ic / ic_sum
        return ranks.dot(weights)

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

        if len(universe) < 100:
            print(f'[debug] signals=0 (universe too small: {len(universe)})', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        factor_df = self._compute_factors(prices, universe)
        if factor_df.empty:
            print('[debug] signals=0 (factor_df empty)', file=sys.stderr)
            return []

        scores = self._lasso_select_score(factor_df)
        if scores.empty:
            print('[debug] signals=0 (scores empty)', file=sys.stderr)
            return []

        n       = len(scores)
        decile  = max(1, n // 10)
        pos_pct = round(scale * 0.02, 4)

        signals: List[Signal] = []

        for ticker in scores.nlargest(decile).index[: self.MAX_SIGNALS // 2]:
            if ticker not in prices.columns:
                continue
            series = prices[ticker].dropna()
            if series.empty:
                continue
            price = float(series.iloc[-1])
            st = self.compute_stops_and_targets(series, 'LONG', price, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker, direction='LONG',
                entry_price=price, stop_loss=st['stop'],
                target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=pos_pct, confidence='MED',
                signal_params={'score': float(scores[ticker])},
            ))

        for ticker in scores.nsmallest(decile).index[: self.MAX_SIGNALS // 2]:
            if ticker not in prices.columns:
                continue
            series = prices[ticker].dropna()
            if series.empty:
                continue
            price = float(series.iloc[-1])
            st = self.compute_stops_and_targets(series, 'SHORT', price, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker, direction='SHORT',
                entry_price=price, stop_loss=st['stop'],
                target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=pos_pct, confidence='MED',
                signal_params={'score': float(scores[ticker])},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
