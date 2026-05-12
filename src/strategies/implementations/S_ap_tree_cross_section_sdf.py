"""
AP-Tree Cross-Section SDF Strategy
Bryzgalova, Pelger, Zhu (2025) — "Forest through the Trees"

Decision trees (AP-Trees) optimally partition stocks into characteristic-sorted leaf
portfolios that span the SDF, capturing characteristic interactions with up to 3x
higher OOS Sharpe than single/double sorts.
"""
from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['APTreeCrossSectionSDF']


class APTreeCrossSectionSDF(BaseStrategy):
    """AP-Tree recursive binary splits on stock characteristics to span the SDF."""

    id          = 'S_ap_tree_cross_section_sdf'
    name        = 'APTreeCrossSectionSDF'
    description = (
        'AP-Tree characteristic splits rank stocks into SDF-spanning leaf portfolios; '
        'LONG top leaves, SHORT bottom leaves — monthly rebalance.'
    )
    tier               = 2
    signal_frequency   = 'monthly'
    min_lookback       = 1260
    active_in_regimes  = ['LOW_VOL', 'TRANSITIONING']

    TREE_DEPTH = 3
    TOP_K      = 2
    BOTTOM_K   = 2

    def default_parameters(self) -> dict:
        return {
            'tree_depth':  self.TREE_DEPTH,
            'top_k':       self.TOP_K,
            'bottom_k':    self.BOTTOM_K,
            'max_signals': self.MAX_SIGNALS,
        }

    def _build_chars(self, prices: pd.DataFrame, financials, universe: List[str]) -> pd.DataFrame:
        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            return pd.DataFrame()
        px = prices[tickers].iloc[-252:]
        ret_1m   = px.pct_change().iloc[-1]
        ret_12m  = px.pct_change(252).iloc[-1]
        mom_12_1 = ret_12m - ret_1m
        vol_60   = px.pct_change().rolling(60).std().iloc[-1]
        chars = pd.DataFrame({'mom_1m': ret_1m, 'mom_12_1': mom_12_1, 'vol_60': vol_60}, index=tickers)
        chars = chars.dropna(subset=['mom_12_1'])
        if financials is not None and not financials.empty:
            try:
                latest = (
                    financials[financials['ticker'].isin(tickers)]
                    .sort_values('date').groupby('ticker').last()
                )
                for col in ('book_to_market', 'roa', 'asset_growth'):
                    if col in latest.columns:
                        chars[col] = latest[col]
            except Exception:
                pass
        return chars

    @staticmethod
    def _leaf_sharpe(vals: np.ndarray) -> float:
        return float(vals.mean() / (vals.std() + 1e-9))

    def _best_split(self, subset: pd.DataFrame, col: str, scores: pd.Series):
        vals = subset[col].dropna().sort_values().unique()
        thresholds = vals[::max(1, len(vals) // 20)]
        best_gain, best_tau = -np.inf, None
        for tau in thresholds:
            lo = scores[subset[col] < tau]
            hi = scores[subset[col] >= tau]
            if len(lo) < 5 or len(hi) < 5:
                continue
            gain = self._leaf_sharpe(hi.values) - self._leaf_sharpe(lo.values)
            if gain > best_gain:
                best_gain, best_tau = gain, tau
        return best_tau

    def _build_tree(self, chars: pd.DataFrame, score_col: str, depth: int) -> pd.Series:
        leaf_ids = pd.Series(0, index=chars.index)
        feature_cols = [c for c in chars.columns if c != score_col]
        for _ in range(depth):
            new_ids = leaf_ids.copy()
            for leaf in leaf_ids.unique():
                mask   = leaf_ids == leaf
                subset = chars.loc[mask]
                scores = subset[score_col]
                if len(subset) < 10:
                    continue
                best_gain, best_tau, best_col = -np.inf, None, None
                for col in feature_cols:
                    if col not in subset.columns:
                        continue
                    tau = self._best_split(subset, col, scores)
                    if tau is None:
                        continue
                    lo = scores[subset[col] < tau]
                    hi = scores[subset[col] >= tau]
                    if len(lo) < 5 or len(hi) < 5:
                        continue
                    gain = self._leaf_sharpe(hi.values) - self._leaf_sharpe(lo.values)
                    if gain > best_gain:
                        best_gain, best_tau, best_col = gain, tau, col
                if best_col is None:
                    continue
                new_ids[mask & (chars[best_col] >= best_tau)] = leaf * 2 + 1
                new_ids[mask & (chars[best_col] <  best_tau)] = leaf * 2
            leaf_ids = new_ids
        return leaf_ids

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
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

        scale      = self.position_scale(regime_state)
        financials = (aux_data or {}).get('financials')
        chars      = self._build_chars(prices, financials, universe)
        if len(chars) < 50:
            print('[debug] signals=0', file=sys.stderr)
            return []

        chars['_score'] = chars['mom_12_1']
        depth    = int(self.parameters.get('tree_depth',  self.TREE_DEPTH))
        top_k    = int(self.parameters.get('top_k',       self.TOP_K))
        bottom_k = int(self.parameters.get('bottom_k',    self.BOTTOM_K))
        max_sig  = int(self.parameters.get('max_signals', self.MAX_SIGNALS))

        leaf_ids   = self._build_tree(chars, '_score', depth)
        leaf_means = chars['_score'].groupby(leaf_ids).mean().sort_values(ascending=False)
        long_leaves  = set(leaf_means.index[:top_k])
        short_leaves = set(leaf_means.index[-bottom_k:])

        signals: List[Signal] = []
        for ticker in chars.index:
            leaf = leaf_ids.get(ticker)
            if leaf is None or (leaf not in long_leaves and leaf not in short_leaves):
                continue
            if ticker not in prices.columns:
                continue
            px_series = prices[ticker].dropna()
            if px_series.empty:
                continue
            current_price = float(px_series.iloc[-1])
            if current_price <= 0:
                continue
            direction  = 'LONG' if leaf in long_leaves else 'SHORT'
            st         = self.compute_stops_and_targets(
                px_series, direction, current_price, regime_state=regime_state
            )
            leaf_size  = int((leaf_ids == leaf).sum())
            size_pct   = round(scale * 0.10 / max(leaf_size, 1), 4)
            confidence = 'HIGH' if leaf in long_leaves else 'MED'
            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = round(current_price, 4),
                stop_loss         = st['stop'],
                target_1          = st['t1'],
                target_2          = st['t2'],
                target_3          = st['t3'],
                position_size_pct = size_pct,
                confidence        = confidence,
                signal_params     = {
                    'leaf_id':    int(leaf),
                    'leaf_mean':  round(float(leaf_means.get(leaf, 0.0)), 6),
                    'tree_depth': depth,
                },
            ))
            if len(signals) >= max_sig:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
