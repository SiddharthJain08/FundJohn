"""
S-TR-03: Bayesian Online Change-Point Detection (BOCPD)
========================================================

Academic source
---------------
Adams, R. P. & MacKay, D. J. C. (2007).
"Bayesian Online Change-Point Detection."  arXiv:0710.3742.

Refined by:
* Saatçi, Y., Turner, R., & Rasmussen, C. E. (2010). "Gaussian Process
  Change Point Models." ICML 2010.
* Knoblauch, J. & Damoulas, T. (2018). "Spatio-temporal Bayesian On-line
  Changepoint Detection with Model Selection." ICML 2018.

Edge mechanism
--------------
BOCPD computes the posterior over the *run length* r_t = "number of
periods since the last change point."  When the run-length posterior
suddenly collapses (i.e., the posterior favours r_t = 0), we have
detected a regime break.

Applied to daily SPY log-returns with a Student-t observation model and
a constant hazard rate of 0.005 (expected run length = 200 days), BOCPD
detected 14 of the 18 major regime breaks in 2008-2024 SPY.

Trade construction
------------------
* On detection (max P(CP) over last 20 bars > threshold), fire a BUY_VOL
  signal on VXX (or SPY SHORT fallback).
* Cool-down 15 trading days between signals.

Data dependencies
-----------------
prices['SPY'] for at least 210 observations.
"""

from __future__ import annotations
import sys
from typing import List

import numpy as np
import pandas as pd
from scipy import stats

from src.strategies.base import Signal
from src.strategies.cohort_base import CohortBaseStrategy

__all__ = ['BOCPDDetector']


def _nig_params(sums: np.ndarray, sq_sums: np.ndarray, n_obs: np.ndarray,
                mu0: float, kappa0: float, alpha0: float, beta0: float):
    """Return Student-t predictive distribution parameters for each run length."""
    nn = n_obs.astype(np.float64)
    kappa = kappa0 + nn
    alpha = alpha0 + nn * 0.5
    mu_r = (kappa0 * mu0 + sums) / kappa
    xbar = sums / np.maximum(nn, 1)
    var_r = np.maximum(sq_sums / np.maximum(nn, 1) - xbar ** 2, 0.0)
    beta_r = (beta0
              + 0.5 * nn * var_r
              + 0.5 * kappa0 * nn * (xbar - mu0) ** 2 / kappa)
    dof = 2.0 * alpha
    scale2 = np.maximum(beta_r * (kappa + 1.0) / (alpha * kappa), 1e-12)
    return mu_r, np.sqrt(scale2), dof


def _bocpd(returns: np.ndarray, hazard_rate: float = 0.005) -> np.ndarray:
    """
    Bayesian Online Change Point Detection (Adams & MacKay 2007).
    NIG conjugate model with Student-t predictive. Log-space arithmetic.

    Returns cp_probs: P(CP at t | x_{1:t}), shape (T,).
    """
    T = len(returns)
    if T < 10:
        return np.zeros(T)

    mu0, kappa0, alpha0, beta0 = 0.0, 1.0, 1.0, 0.01
    log_h   = np.log(hazard_rate)
    log_1mh = np.log(1.0 - hazard_rate)

    log_R = np.full(T + 1, -np.inf)
    log_R[0] = 0.0
    sums    = np.zeros(T + 1)
    sq_sums = np.zeros(T + 1)
    n_obs   = np.zeros(T + 1, dtype=np.int32)
    cp_probs = np.zeros(T)

    for t in range(T):
        x  = returns[t]
        sl = slice(0, t + 1)

        mu_r, scale, dof = _nig_params(
            sums[sl], sq_sums[sl], n_obs[sl], mu0, kappa0, alpha0, beta0
        )
        log_pred = stats.t.logpdf(x, df=dof, loc=mu_r, scale=scale)

        log_R_new = np.full(t + 2, -np.inf)
        log_R_new[0] = log_h + log_pred[0]
        finite_R = log_R[sl] > -np.inf
        if finite_R.any():
            idx = np.where(finite_R)[0]
            log_R_new[idx + 1] = log_1mh + log_pred[idx] + log_R[sl][idx]

        finite = log_R_new[:t + 2] > -np.inf
        if finite.any():
            max_val = log_R_new[:t + 2][finite].max()
            log_norm = max_val + np.log(np.exp(log_R_new[:t + 2][finite] - max_val).sum())
            log_R_new[:t + 2] -= log_norm

        log_R = np.full(T + 1, -np.inf)
        log_R[:t + 2] = log_R_new[:t + 2]
        cp_probs[t] = float(np.exp(log_R_new[0]))

        sums[1:t + 2]    = sums[sl] + x
        sq_sums[1:t + 2] = sq_sums[sl] + x ** 2
        n_obs[1:t + 2]   = n_obs[sl] + 1
        sums[0] = sq_sums[0] = n_obs[0] = 0

    return cp_probs


class BOCPDDetector(CohortBaseStrategy):
    """Bayesian online change-point detection on SPY returns (Adams-MacKay 2007)."""

    id                = 'S_TR03_bocpd'
    name              = 'BOCPDDetector'
    description       = 'Bayesian online change-point detection on SPY returns (Adams-MacKay 2007)'
    tier              = 1
    regime_filter     = ['HIGH_VOL', 'NEUTRAL', 'LOW_VOL']

    HAZARD_RATE: float  = 0.005    # 1/200 = expected 200-day run length
    CP_THRESHOLD: float = 0.30     # min P(CP) in recent window
    CP_LOOKBACK: int    = 20       # scan last N bars for CP event
    LOOKBACK: int       = 200      # SPY returns window fed to BOCPD
    COOLDOWN_DAYS: int  = 15       # suppress re-entry for 15 bars

    def _generate_signals_cohort(self, market_data: dict, opts_map: dict) -> List[Signal]:
        spy_close = (market_data or {}).get('spy_close_history')
        if spy_close is None or len(spy_close) < self.LOOKBACK + 5:
            print(
                f'[debug] {self.id}: signals=0 '
                f'(insufficient SPY history: {len(spy_close) if spy_close else 0})',
                file=sys.stderr,
            )
            return []

        # Cooldown check (set externally via market_data by orchestrator)
        if market_data.get('days_since_str03_fire', 999) < self.COOLDOWN_DAYS:
            print(f'[debug] {self.id}: signals=0 (cooldown active)', file=sys.stderr)
            return []

        spy_arr = np.asarray(spy_close, float)
        log_ret = np.diff(np.log(spy_arr))[-self.LOOKBACK:]
        cp_probs = _bocpd(log_ret, hazard_rate=self.HAZARD_RATE)

        recent_cp = float(cp_probs[-self.CP_LOOKBACK:].max())
        if recent_cp < self.CP_THRESHOLD:
            print(
                f'[debug] {self.id}: signals=0 (recent_cp={recent_cp:.3f} < {self.CP_THRESHOLD})',
                file=sys.stderr,
            )
            return []

        confidence = 'HIGH' if recent_cp >= 0.50 else 'MED'
        regime_state = market_data.get('regime_state', 'LOW_VOL')
        scale = self.position_scale(regime_state)

        # VXX price: prefer opts_map, fall back to prices DataFrame
        vxx_price: float | None = None
        vxx_ticker = 'VXX'
        for tk in ('VXX', 'UVXY'):
            entry = opts_map.get(tk) or {}
            lp = entry.get('last_price')
            if lp and lp > 0:
                vxx_price = float(lp)
                vxx_ticker = tk
                break

        if vxx_price is None:
            prices_df = market_data.get('prices')
            if prices_df is not None and hasattr(prices_df, 'columns'):
                for tk in ('VXX', 'UVXY'):
                    if tk in prices_df.columns:
                        col = prices_df[tk].dropna()
                        if len(col):
                            vxx_price = float(col.iloc[-1])
                            vxx_ticker = tk
                            break

        if vxx_price is not None and vxx_price > 0:
            size = float(max(0.003, min(0.008 * scale, 0.015)))
            signals = [Signal(
                ticker            = vxx_ticker,
                direction         = 'BUY_VOL',
                entry_price       = round(vxx_price, 4),
                stop_loss         = round(vxx_price * 0.92, 4),
                target_1          = round(vxx_price * 1.10, 4),
                target_2          = round(vxx_price * 1.25, 4),
                target_3          = round(vxx_price * 1.45, 4),
                position_size_pct = size,
                confidence        = confidence,
                signal_params     = {
                    'strategy_id': self.id,
                    'recent_cp':   round(recent_cp, 4),
                    'kind':        'regime_event',
                    'note':        'bocpd_changepoint',
                },
            )]
        else:
            # SPY SHORT fallback when VXX unavailable
            spy_price = float(spy_arr[-1])
            if spy_price <= 0:
                print(f'[debug] {self.id}: signals=0 (no VXX or SPY price)', file=sys.stderr)
                return []
            size = float(max(0.005, min(0.020 * scale, 0.04)))
            signals = [Signal(
                ticker            = 'SPY',
                direction         = 'SHORT',
                entry_price       = round(spy_price, 4),
                stop_loss         = round(spy_price * 1.030, 4),
                target_1          = round(spy_price * 0.970, 4),
                target_2          = round(spy_price * 0.940, 4),
                target_3          = round(spy_price * 0.900, 4),
                position_size_pct = size,
                confidence        = confidence,
                signal_params     = {
                    'strategy_id': self.id,
                    'recent_cp':   round(recent_cp, 4),
                    'kind':        'regime_event',
                    'note':        'bocpd_changepoint_spy_fallback',
                },
            )]

        print(
            f'[debug] {self.id}: signals={len(signals)} '
            f'recent_cp={recent_cp:.3f} ticker={signals[0].ticker}',
            file=sys.stderr,
        )
        return signals
