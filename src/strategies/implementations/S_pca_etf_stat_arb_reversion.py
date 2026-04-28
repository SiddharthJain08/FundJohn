from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['PCAETFStatArbReversion']


class PCAETFStatArbReversion(BaseStrategy):
    """Idiosyncratic OU residuals from PCA factor regression; contrarian s-score signals."""

    id          = 'S_pca_etf_stat_arb_reversion'
    name        = 'PCAETFStatArbReversion'
    description = 'Idiosyncratic OU residuals from PCA factor regression; contrarian s-score signals.'
    tier        = 2
    min_lookback = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    N_FACTORS  = 15
    LOOKBACK   = 252
    OU_WINDOW  = 60
    S_ENTRY    = 1.25
    BASE_SIZE  = 0.004   # 0.4% per name

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

        # Filter to universe tickers present in prices
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 50:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        # Use last LOOKBACK rows, drop columns with too many NaNs
        price_mat = prices[tickers].tail(self.LOOKBACK).dropna(axis=1, thresh=self.LOOKBACK // 2)
        if price_mat.shape[0] < self.OU_WINDOW + 20:
            print(f'[debug] signals=0 (insufficient rows: {price_mat.shape[0]})', file=sys.stderr)
            return []

        tickers = list(price_mat.columns)
        ret_mat = price_mat.pct_change().dropna()
        if ret_mat.shape[0] < self.OU_WINDOW + 10:
            print(f'[debug] signals=0 (insufficient return rows)', file=sys.stderr)
            return []

        # PCA via SVD on demeaned returns — avoids covariance inversion
        R = ret_mat.values           # (T, N)
        R_dm = R - R.mean(axis=0)
        n_factors = min(self.N_FACTORS, R_dm.shape[1] - 1, R_dm.shape[0] - 1)
        try:
            U, sv, Vt = np.linalg.svd(R_dm, full_matrices=False)
        except np.linalg.LinAlgError:
            print(f'[debug] signals=0 (SVD failed)', file=sys.stderr)
            return []

        factors = U[:, :n_factors] * sv[:n_factors]   # (T, K) eigenportfolio returns

        # OLS: regress each stock on factors → idiosyncratic residuals
        XtX = factors.T @ factors   # (K, K)
        try:
            XtX_inv = np.linalg.inv(XtX)
        except np.linalg.LinAlgError:
            XtX_inv = np.linalg.pinv(XtX)
        Xty   = factors.T @ R        # (K, N)
        betas = XtX_inv @ Xty        # (K, N)
        resid = R - factors @ betas  # (T, N) idiosyncratic returns

        # Cumulative residuals as OU process proxy
        X_ou = np.cumsum(resid, axis=0)   # (T, N)

        # Fit OU over last OU_WINDOW days (vectorised OLS: dX = a + b*X_lag)
        X_w  = X_ou[-self.OU_WINDOW:]    # (W, N)
        dX   = np.diff(X_w, axis=0)      # (W-1, N)
        Xlag = X_w[:-1]                  # (W-1, N)

        n = len(dX)
        sx  = Xlag.sum(axis=0)
        sy  = dX.sum(axis=0)
        sxx = (Xlag ** 2).sum(axis=0)
        sxy = (Xlag * dX).sum(axis=0)
        det = n * sxx - sx ** 2
        det = np.where(np.abs(det) < 1e-12, 1e-12, det)

        b     = (n * sxy - sx * sy) / det    # slope  ≈ -kappa
        a     = (sy - b * sx) / n            # intercept ≈ kappa * m
        kappa = -b
        m     = np.where(np.abs(kappa) > 1e-8, a / kappa, 0.0)

        ou_resid   = dX - (a[np.newaxis, :] + b[np.newaxis, :] * Xlag)
        sigma      = ou_resid.std(axis=0)
        safe_kappa = np.where(kappa > 0, kappa, np.nan)
        sigma_eq   = sigma / np.sqrt(2.0 * safe_kappa)

        X_cur  = X_ou[-1]   # (N,)
        s_score = (X_cur - m) / np.where(sigma_eq > 0, sigma_eq, np.nan)

        latest_prices = price_mat.iloc[-1]
        ranked_idx    = np.argsort(np.abs(np.nan_to_num(s_score)))[::-1]

        signals: List[Signal] = []
        for idx in ranked_idx:
            if len(signals) >= self.MAX_SIGNALS:
                break
            ss = s_score[idx]
            if np.isnan(ss) or kappa[idx] <= 0 or np.isnan(sigma_eq[idx]) or sigma_eq[idx] <= 0:
                continue

            ticker = tickers[idx]
            px = float(latest_prices.get(ticker, np.nan))
            if np.isnan(px) or px <= 0:
                continue

            if ss < -self.S_ENTRY:
                direction = 'LONG'
            elif ss > self.S_ENTRY:
                direction = 'SHORT'
            else:
                continue

            st   = self.compute_stops_and_targets(price_mat[ticker], direction, px, regime_state=regime_state)
            conf = 'HIGH' if abs(ss) > 2.0 else ('MED' if abs(ss) > 1.5 else 'LOW')

            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=round(px, 4),
                stop_loss=round(float(st['stop']), 4),
                target_1=round(float(st['t1']), 4),
                target_2=round(float(st['t2']), 4),
                target_3=round(float(st['t3']), 4),
                position_size_pct=round(self.BASE_SIZE * scale, 6),
                confidence=conf,
                signal_params={
                    's_score': round(float(ss), 4),
                    'kappa':   round(float(kappa[idx]), 6),
                    'sigma_eq': round(float(sigma_eq[idx]), 6),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
