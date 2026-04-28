from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['ThreeDPCACharacteristicFactors']


class ThreeDPCACharacteristicFactors(BaseStrategy):
    """Restricted PCA on 3D characteristic panel → rank by PC1 loading → LONG top decile, SHORT bottom decile monthly."""

    id               = 'S_3d_pca_characteristic_factors'
    name             = 'ThreeDPCACharacteristicFactors'
    description      = ('Proportionality-restricted PCA on 3D double-sorted characteristic portfolios yields '
                        'parsimonious factors; rank stocks by PC1 score → LONG top decile, SHORT bottom decile, monthly.')
    tier             = 2
    min_lookback     = 252
    signal_frequency = 'monthly'
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    _DECILE_PCT = 0.10
    _BASE_SIZE  = 0.035   # per-position base before regime scale

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

        # Monthly rebalance gate
        if isinstance(prices.index, pd.DatetimeIndex) and len(prices.index) >= 2:
            if prices.index[-1].month == prices.index[-2].month:
                return []

        scale    = self.position_scale(regime_state)
        tickers  = [t for t in universe if t in prices.columns]
        if len(tickers) < 30:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        prices_sub = prices[tickers].copy()
        latest     = prices_sub.iloc[-1]
        chars: dict[str, pd.Series] = {}

        # ── Price-based characteristics ──────────────────────────────────
        if len(prices_sub) >= 252:
            mom  = (prices_sub.iloc[-21] / prices_sub.iloc[-252]) - 1
            chars['mom12'] = mom.replace([np.inf, -np.inf], np.nan)

        if len(prices_sub) >= 21:
            rev = -((latest / prices_sub.iloc[-21]) - 1)
            chars['rev1'] = rev.replace([np.inf, -np.inf], np.nan)

        if len(prices_sub) >= 63:
            chars['vol12'] = -(prices_sub.pct_change().iloc[-63:].std())

        # ── Financials-based characteristics (dict[ticker → dict] format) ──
        financials = (aux_data or {}).get('financials') or {}
        if isinstance(financials, dict) and financials:
            bm_vals, roe_vals, inv_vals = {}, {}, {}
            for t in tickers:
                fin = financials.get(t)
                if not fin:
                    continue
                p = float(latest.get(t, 0) or 0)
                if p <= 0:
                    continue
                bv   = fin.get('totalStockholdersEquity') or fin.get('bookValue')
                ni   = fin.get('returnOnEquity') or fin.get('netIncome')
                ta   = fin.get('totalAssetsGrowth')
                if bv is not None:
                    v = float(bv) / p
                    if np.isfinite(v):
                        bm_vals[t] = v
                if ni is not None:
                    v = float(ni)
                    if np.isfinite(v):
                        roe_vals[t] = v
                if ta is not None:
                    v = -float(ta)   # low investment → higher rank
                    if np.isfinite(v):
                        inv_vals[t] = v
            if bm_vals:
                chars['bm']  = pd.Series(bm_vals)
            if roe_vals:
                chars['roe'] = pd.Series(roe_vals)
            if inv_vals:
                chars['inv'] = pd.Series(inv_vals)

        if len(chars) < 2:
            print(f'[debug] signals=0 (insufficient characteristics: {list(chars.keys())})', file=sys.stderr)
            return []

        # ── Cross-sectional rank each characteristic ──────────────────────
        char_ranks: dict[str, pd.Series] = {}
        for cname, series in chars.items():
            s = series.reindex(tickers)
            if s.dropna().__len__() >= 20:
                char_ranks[cname] = s.rank(pct=True)

        if len(char_ranks) < 2:
            print(f'[debug] signals=0 (not enough ranked chars: {len(char_ranks)})', file=sys.stderr)
            return []

        rank_df = pd.DataFrame(char_ranks).reindex(tickers).dropna(thresh=2)
        if len(rank_df) < 30:
            print(f'[debug] signals=0 (rank_df rows={len(rank_df)})', file=sys.stderr)
            return []

        # ── Restricted PCA approximation ──────────────────────────────────
        # Proportionality restriction → group chars by factor family, equal-weight
        # within each group (approximates Kronecker block structure from §3).
        groups = {
            'momentum': [c for c in ('mom12',)       if c in rank_df.columns],
            'reversal': [c for c in ('rev1',)         if c in rank_df.columns],
            'quality':  [c for c in ('roe', 'bm')    if c in rank_df.columns],
            'risk':     [c for c in ('vol12', 'inv')  if c in rank_df.columns],
        }
        group_scores: dict[str, pd.Series] = {}
        for gname, cols in groups.items():
            if cols:
                group_scores[gname] = rank_df[cols].mean(axis=1)

        if not group_scores:
            print(f'[debug] signals=0 (no group scores)', file=sys.stderr)
            return []

        score_df = pd.DataFrame(group_scores).fillna(0.5)
        X = score_df.values - score_df.values.mean(axis=0)

        if X.shape[0] >= X.shape[1] + 5:
            try:
                _, _, Vt = np.linalg.svd(X, full_matrices=False)
                composite = pd.Series(X @ Vt[0], index=score_df.index)
            except np.linalg.LinAlgError:
                composite = score_df.mean(axis=1)
        else:
            composite = score_df.mean(axis=1)

        composite = composite.rank(pct=True)

        # ── Decile signals ────────────────────────────────────────────────
        n_dec         = max(1, int(len(composite) * self._DECILE_PCT))
        long_tickers  = composite.nlargest(n_dec).index.tolist()
        short_tickers = composite.nsmallest(n_dec).index.tolist()
        pos_size      = min(self._BASE_SIZE * scale, 0.10)
        signals: List[Signal] = []
        half_cap = self.MAX_SIGNALS // 2

        for ticker in long_tickers[:half_cap]:
            price = float(latest.get(ticker, np.nan))
            if np.isnan(price) or price <= 0:
                continue
            st = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'LONG', price, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker, direction='LONG',
                entry_price=round(price, 4),
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=pos_size, confidence='MED',
                signal_params={'factor': 'pc1_3d_pca', 'composite_rank': float(composite[ticker])},
            ))

        for ticker in short_tickers[:half_cap]:
            price = float(latest.get(ticker, np.nan))
            if np.isnan(price) or price <= 0:
                continue
            st = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'SHORT', price, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker, direction='SHORT',
                entry_price=round(price, 4),
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=pos_size, confidence='MED',
                signal_params={'factor': 'pc1_3d_pca', 'composite_rank': float(composite[ticker])},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]

