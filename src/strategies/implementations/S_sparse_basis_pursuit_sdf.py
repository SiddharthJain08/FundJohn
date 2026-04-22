from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['SparseBasisPursuitSdf']


class SparseBasisPursuitSdf(BaseStrategy):
    """L1-sparse SDF via random Fourier feature expansion of fundamental+momentum characteristics."""

    id          = 'S_sparse_basis_pursuit_sdf'
    name        = 'SparseBasisPursuitSdf'
    description = 'L1-sparse SDF via random Fourier feature expansion of fundamental+momentum characteristics'
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    min_lookback = 504

    def default_parameters(self):
        return {
            'n_rff':       24,    # random Fourier features
            'lasso_alpha': 0.05,  # L1 sparsity penalty
            'top_n':       10,    # LONG positions to take
            'base_size':   0.05,  # base position fraction
            'seed':        42,
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
            print(f'[debug] signals=0 (regime={regime_state} not active)', file=sys.stderr)
            return []
        scale = self.position_scale(regime_state)
        p = self.parameters

        try:
            feat_df = self._build_features(prices, aux_data, universe)
            if feat_df is None or len(feat_df) < 10:
                n = 0 if feat_df is None else len(feat_df)
                print(f'[debug] signals=0 (insufficient features rows={n})', file=sys.stderr)
                return []

            rff_df = self._random_fourier_features(feat_df, p['n_rff'], p['seed'])
            scores = self._sparse_score(rff_df, feat_df, p['lasso_alpha'])
            top_tickers = scores.nlargest(p['top_n']).index.tolist()
        except Exception as e:
            print(f'[debug] signals=0 (error: {e})', file=sys.stderr)
            return []

        signals: List[Signal] = []
        for ticker in top_tickers:
            if ticker not in prices.columns:
                continue
            series = prices[ticker].dropna()
            if len(series) < 20:
                continue
            current_price = float(series.iloc[-1])
            if current_price <= 0:
                continue
            score_val = float(scores.get(ticker, 0.5))
            confidence = 'HIGH' if score_val > 0.75 else ('MED' if score_val > 0.45 else 'LOW')
            stops = self.compute_stops_and_targets(
                series, 'LONG', current_price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=current_price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(p['base_size'] * scale, 4),
                confidence=confidence,
                signal_params={
                    'sdf_score': round(score_val, 4),
                    'regime': regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_features(self, prices: pd.DataFrame, aux_data, universe) -> 'pd.DataFrame | None':
        """Build cross-sectional characteristic matrix."""
        valid = [t for t in universe if t in prices.columns]
        if not valid:
            return None
        close = prices[valid]
        if len(close) < 21:
            return None

        rows = {}
        for t in valid:
            s = close[t].dropna()
            if len(s) < 21 or float(s.iloc[-1]) <= 0:
                continue
            rows[t] = {
                'mom_1m':  float(s.iloc[-1] / s.iloc[max(-21, -len(s))] - 1),
                'mom_3m':  float(s.iloc[-1] / s.iloc[max(-63, -len(s))] - 1),
                'mom_6m':  float(s.iloc[-1] / s.iloc[max(-126, -len(s))] - 1),
                'mom_12m': float(s.iloc[-1] / s.iloc[max(-252, -len(s))] - 1),
                'vol_21d': float(s.pct_change().tail(21).std()) if len(s) >= 22 else 0.02,
            }

        if not rows:
            return None
        feat_df = pd.DataFrame(rows).T

        # Merge fundamentals
        if aux_data and isinstance(aux_data.get('financials'), pd.DataFrame):
            fin = aux_data['financials']
            fin_cols = [c for c in ['gross_margin', 'roe', 'roic', 'revenue_growth',
                                    'pe_ratio', 'ev_ebitda', 'net_margin', 'debt_equity_ratio']
                        if c in fin.columns]
            if 'ticker' in fin.columns and fin_cols:
                key_col = 'date' if 'date' in fin.columns else None
                fin_latest = (fin.sort_values(key_col).groupby('ticker')[fin_cols].last()
                              if key_col else fin.groupby('ticker')[fin_cols].last())
                feat_df = feat_df.join(fin_latest, how='left')

        feat_df = feat_df.fillna(0.0).clip(-10, 10)
        return feat_df if len(feat_df) >= 10 else None

    def _random_fourier_features(self, feat_df: pd.DataFrame, n_rff: int, seed: int) -> pd.DataFrame:
        """RFF nonlinear expansion: phi(x) = sqrt(2/R) * cos(Omega x + b)."""
        X = feat_df.values.astype(float)
        rng = np.random.RandomState(seed)
        omega = rng.randn(X.shape[1], n_rff)
        bias  = rng.uniform(0, 2 * np.pi, n_rff)
        rff   = np.sqrt(2.0 / n_rff) * np.cos(X @ omega + bias)
        return pd.DataFrame(rff, index=feat_df.index)

    def _sparse_score(self, rff_df: pd.DataFrame, feat_df: pd.DataFrame, alpha: float) -> pd.Series:
        """Lasso (L1) sparse regression on RFF features -> percentile-ranked scores."""
        from sklearn.linear_model import Lasso
        X = rff_df.values.astype(float)
        # Target: cross-sectional 12-1 momentum rank as proxy for SDF mean structure
        y_raw = feat_df['mom_12m'].values if 'mom_12m' in feat_df.columns else X[:, 0]
        y = (y_raw - y_raw.mean()) / (y_raw.std() + 1e-8)
        model = Lasso(alpha=alpha, fit_intercept=True, max_iter=1000)
        model.fit(X, y)
        raw = pd.Series(model.predict(X), index=rff_df.index)
        return raw.rank(pct=True)
