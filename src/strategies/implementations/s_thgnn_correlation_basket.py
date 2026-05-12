"""
S_thgnn_correlation_basket — Spectral-clustering basket strategy.

Approximates THGNN paper (Fanshawe et al. 2026): rolling Fisher-z correlation
matrix → normalized graph Laplacian spectral embedding → k-cluster baskets →
contrarian within-basket positioning (LONG laggards, SHORT leaders).
Rebalances every 10 trading days.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['THGNNCorrelationBasket']


class THGNNCorrelationBasket(BaseStrategy):
    """THGNN-inspired spectral-cluster baskets; contrarian within-basket positioning."""

    id          = 'S_thgnn_correlation_basket'
    name        = 'THGNNCorrelationBasket'
    description = ('Spectral-cluster baskets via Fisher-z rolling correlation; '
                   'LONG within-basket laggards, SHORT within-basket leaders.')
    tier        = 2
    min_lookback = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    # Class-level defaults — overridable via DB parameters
    _LOOKBACK      = 30
    _MOM_DAYS      = 5
    _N_CLUSTERS    = 8
    _REBAL_PERIOD  = 10
    _MIN_BASKET    = 5
    _BASE_SIZE_PCT = 0.02

    def default_parameters(self) -> dict:
        return {
            'lookback':      self._LOOKBACK,
            'momentum_days': self._MOM_DAYS,
            'n_clusters':    self._N_CLUSTERS,
            'rebal_period':  self._REBAL_PERIOD,
            'min_basket':    self._MIN_BASKET,
        }

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

        p = self.parameters
        lookback      = int(p.get('lookback',      self._LOOKBACK))
        mom_days      = int(p.get('momentum_days', self._MOM_DAYS))
        n_clusters    = int(p.get('n_clusters',    self._N_CLUSTERS))
        rebal_period  = int(p.get('rebal_period',  self._REBAL_PERIOD))
        min_basket    = int(p.get('min_basket',    self._MIN_BASKET))

        # Only rebalance every rebal_period-th trading day
        if len(prices) % rebal_period != 0:
            print(f'[debug] signals=0 (non-rebal day {len(prices)})', file=sys.stderr)
            return []

        # Filter to tickers present in prices
        tickers = [t for t in (universe or []) if t in prices.columns]
        needed  = lookback + mom_days + 5
        if len(tickers) < 20 or len(prices) < needed:
            print(f'[debug] signals=0 (universe={len(tickers)} rows={len(prices)})', file=sys.stderr)
            return []

        # Recent slice, drop sparsely-covered tickers
        px = prices[tickers].iloc[-(lookback + mom_days + 5):]
        px = px.dropna(axis=1, thresh=lookback)
        tickers = px.columns.tolist()
        if len(tickers) < 20:
            print(f'[debug] signals=0 (post-NaN filter: {len(tickers)})', file=sys.stderr)
            return []

        rets = px.pct_change().dropna()

        # ── Fisher-z correlation (30-day window) ──────────────────────────────
        corr_raw = rets.iloc[-lookback:].corr().fillna(0).values
        np.fill_diagonal(corr_raw, 1.0)
        z_mat    = np.arctanh(np.clip(corr_raw, -0.9999, 0.9999))
        sim_mat  = (np.tanh(z_mat) + 1.0) / 2.0   # scale to [0,1]

        # ── Spectral embedding via normalised graph Laplacian ─────────────────
        deg         = sim_mat.sum(axis=1)
        d_inv_sqrt  = 1.0 / np.sqrt(np.maximum(deg, 1e-10))
        L_sym       = np.eye(len(tickers)) - (d_inv_sqrt[:, None] * sim_mat * d_inv_sqrt[None, :])
        try:
            _, eigvecs = np.linalg.eigh(L_sym)
        except np.linalg.LinAlgError:
            print(f'[debug] signals=0 (eigh failed)', file=sys.stderr)
            return []

        k         = min(n_clusters, len(tickers) - 1)
        embedding = eigvecs[:, :k]
        norms     = np.linalg.norm(embedding, axis=1, keepdims=True)
        embedding = embedding / np.maximum(norms, 1e-10)
        labels    = np.argmax(np.abs(embedding), axis=1)

        # ── 5-day within-basket momentum ranking ──────────────────────────────
        mom_rets     = rets.iloc[-mom_days:].sum()
        ticker_idx   = {t: i for i, t in enumerate(tickers)}
        scale        = self.position_scale(regime_state)
        signals: List[Signal] = []

        for basket_id in range(k):
            basket_tickers = [t for t, lbl in zip(tickers, labels) if lbl == basket_id]
            if len(basket_tickers) < min_basket:
                continue

            ranked   = mom_rets[basket_tickers].sort_values()
            quintile = max(1, len(basket_tickers) // 5)
            longs    = set(ranked.index[:quintile])
            shorts   = set(ranked.index[-quintile:])

            # Mean intra-basket correlation for confidence
            b_idx   = [ticker_idx[t] for t in basket_tickers]
            sub     = corr_raw[np.ix_(b_idx, b_idx)]
            tri_idx = np.triu_indices(len(b_idx), k=1)
            mean_corr = float(sub[tri_idx].mean()) if len(tri_idx[0]) > 0 else 0.3
            confidence = 'HIGH' if mean_corr > 0.5 else ('MED' if mean_corr > 0.25 else 'LOW')

            for ticker in list(longs) + list(shorts):
                direction = 'LONG' if ticker in longs else 'SHORT'
                price_series = px[ticker].dropna()
                if len(price_series) < 2:
                    continue
                current_price = float(price_series.iloc[-1])
                if current_price <= 0:
                    continue
                st = self.compute_stops_and_targets(
                    price_series, direction, current_price, regime_state=regime_state
                )
                signals.append(Signal(
                    ticker=ticker,
                    direction=direction,
                    entry_price=current_price,
                    stop_loss=float(st['stop']),
                    target_1=float(st['t1']),
                    target_2=float(st['t2']),
                    target_3=float(st['t3']),
                    position_size_pct=round(self._BASE_SIZE_PCT * scale, 4),
                    confidence=confidence,
                    signal_params={
                        'basket_id':       int(basket_id),
                        'mom_5d':          round(float(mom_rets.get(ticker, 0.0)), 6),
                        'mean_basket_corr': round(mean_corr, 4),
                    },
                ))
                if len(signals) >= self.MAX_SIGNALS:
                    break
            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
