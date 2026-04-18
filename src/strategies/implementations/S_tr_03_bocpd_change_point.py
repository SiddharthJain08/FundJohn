from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from scipy import stats
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['BOCPDChangePoint']


def _nig_params(sums: np.ndarray, sq_sums: np.ndarray, n_obs: np.ndarray,
                mu0: float, kappa0: float, alpha0: float, beta0: float):
    """Return NIG posterior parameters for each run length."""
    nn     = n_obs.astype(np.float64)
    kappa  = kappa0 + nn
    alpha  = alpha0 + nn * 0.5
    mu_r   = (kappa0 * mu0 + sums) / kappa
    xbar   = sums / np.maximum(nn, 1)
    var_r  = np.maximum(sq_sums / np.maximum(nn, 1) - xbar ** 2, 0.0)
    beta_r = (beta0
              + 0.5 * nn * var_r
              + 0.5 * kappa0 * nn * (xbar - mu0) ** 2 / kappa)
    dof    = 2.0 * alpha
    scale2 = np.maximum(beta_r * (kappa + 1.0) / (alpha * kappa), 1e-12)
    return mu_r, np.sqrt(scale2), dof


def _bocpd(returns: np.ndarray, hazard_rate: float = 0.005) -> tuple[np.ndarray, np.ndarray]:
    """
    Bayesian Online Change Point Detection (Adams & MacKay 2007).
    Normal-Inverse-Gamma conjugate model → Student-t predictive.

    Correct update (Adams & MacKay eq. 3):
        R_t[0]   ∝ H × pred_prior(x_t)          ← prior predictive, NOT joint_sum
        R_t[r+1] ∝ (1-H) × pred_r(x_t) × R_{t-1}[r]

    Returns:
        cp_probs  — P(CP at t | x_{1:t}) = R_t[0], shape (T,)
        run_dist  — final posterior run-length distribution, shape (T+1,)
    """
    T = len(returns)
    if T < 10:
        return np.zeros(T), np.zeros(T + 1)

    mu0, kappa0, alpha0, beta0 = 0.0, 1.0, 1.0, 0.01
    log_h   = np.log(hazard_rate)
    log_1mh = np.log(1.0 - hazard_rate)

    # Work in log space for numerical stability
    log_R    = np.full(T + 1, -np.inf)
    log_R[0] = 0.0  # log(1)
    sums     = np.zeros(T + 1)
    sq_sums  = np.zeros(T + 1)
    n_obs    = np.zeros(T + 1, dtype=np.int32)
    cp_probs = np.zeros(T)

    for t in range(T):
        x  = returns[t]
        sl = slice(0, t + 1)

        mu_r, scale, dof = _nig_params(
            sums[sl], sq_sums[sl], n_obs[sl], mu0, kappa0, alpha0, beta0
        )

        # Log predictive for each run length (absolute PDF values — NOT normalized)
        log_pred = stats.t.logpdf(x, df=dof, loc=mu_r, scale=scale)

        # Adams & MacKay eq. 3:
        #   log P_new[0]   = log(H)   + log_pred[0]               ← prior predictive
        #   log P_new[r+1] = log(1-H) + log_pred[r] + log_R[r]    ← continue
        log_R_new        = np.full(t + 2, -np.inf)
        log_R_new[0]     = log_h + log_pred[0]
        finite_R         = log_R[sl] > -np.inf
        if finite_R.any():
            idx = np.where(finite_R)[0]
            log_R_new[idx + 1] = log_1mh + log_pred[idx] + log_R[sl][idx]

        # Normalise in log space (log-sum-exp)
        finite           = log_R_new[:t + 2] > -np.inf
        max_val          = log_R_new[:t + 2][finite].max()
        log_norm         = max_val + np.log(np.exp(log_R_new[:t + 2][finite] - max_val).sum())
        log_R_new[:t+2] -= log_norm

        log_R                 = np.full(T + 1, -np.inf)
        log_R[:t + 2]         = log_R_new[:t + 2]
        cp_probs[t]           = float(np.exp(log_R_new[0]))

        # Shift sufficient statistics right
        sums[1 : t + 2]    = sums[sl] + x
        sq_sums[1 : t + 2] = sq_sums[sl] + x ** 2
        n_obs[1 : t + 2]   = n_obs[sl] + 1
        sums[0] = sq_sums[0] = n_obs[0] = 0

    return cp_probs, np.exp(log_R)


class BOCPDChangePoint(BaseStrategy):
    """
    BOCPD change-point momentum. Detects statistical regime breaks in daily returns
    (Adams & MacKay 2007, Student-t NIG conjugate). When P(CP in last LOOKBACK_CP
    bars) > CP_THRESHOLD, trades the post-break direction.
    """

    id                = 'S_tr_03_bocpd_change_point'
    name              = 'BOCPDChangePoint'
    description       = 'BOCPD change-point detection — trade post-break direction in TRANSITIONING regime'
    tier              = 1
    active_in_regimes = ['TRANSITIONING']
    min_lookback      = 126  # 6 months for reliable BOCPD calibration

    BOCPD_WINDOW  = 126   # bars fed to BOCPD per ticker
    HAZARD_RATE   = 0.005  # 1/200 ≈ expected 200-day run length
    CP_THRESHOLD  = 0.30   # P(CP) threshold (user spec)
    CP_LOOKBACK   = 20     # scan last N bars for recent CP event (~1 month)
    BASE_SIZE_PCT = 0.012
    VOL_WINDOW    = 21

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

        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            return []

        price_data = prices[tickers].ffill()
        if len(price_data) < self.min_lookback:
            print(f'[debug] {self.id}: signals=0 (need {self.min_lookback} rows)', file=sys.stderr)
            return []

        returns_df = price_data.pct_change().dropna(how='all')
        latest     = price_data.iloc[-1]
        vol        = returns_df.iloc[-self.VOL_WINDOW:].std() * np.sqrt(252)

        scale        = self.position_scale(regime_state)
        signals: List[Signal] = []
        max_per_side = self.MAX_SIGNALS // 2
        long_cands:  list = []
        short_cands: list = []

        for ticker in tickers:
            series = returns_df[ticker].dropna().values
            if len(series) < self.min_lookback:
                continue

            arr            = series[-self.BOCPD_WINDOW:]
            cp_probs, R    = _bocpd(arr, hazard_rate=self.HAZARD_RATE)

            # Recent CP: max probability in last CP_LOOKBACK bars
            recent_cp = float(cp_probs[-self.CP_LOOKBACK:].max())
            if recent_cp < self.CP_THRESHOLD:
                continue

            # Direction: most likely current run length → mean of post-break returns
            most_likely_rl = int(R.argmax())
            if most_likely_rl == 0:
                most_likely_rl = 1
            post_break = arr[-most_likely_rl:]
            post_mean  = float(post_break.mean()) if len(post_break) > 0 else 0.0

            if post_mean > 0:
                long_cands.append((ticker, recent_cp, post_mean))
            elif post_mean < 0:
                short_cands.append((ticker, recent_cp, post_mean))

        # Sort by CP probability (strongest signal first)
        long_cands.sort(key=lambda x: x[1], reverse=True)
        short_cands.sort(key=lambda x: x[1], reverse=True)

        for direction, candidates, max_n in [
            ('LONG',  long_cands[:max_per_side],  max_per_side),
            ('SHORT', short_cands[:max_per_side], max_per_side),
        ]:
            for ticker, cp_prob, post_mean in candidates:
                price = float(latest.get(ticker, 0))
                if price <= 0:
                    continue
                ticker_vol = max(float(vol.get(ticker, 0.20)), 1e-4)
                size = float(self.BASE_SIZE_PCT * (0.15 / ticker_vol) * scale)
                size = max(0.001, min(size, 0.05))

                confidence = 'HIGH' if cp_prob > 0.60 else ('MED' if cp_prob > 0.40 else 'LOW')

                st = self.compute_stops_and_targets(
                    price_data[ticker].dropna(), direction, price,
                    regime_state=regime_state,
                )
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = direction,
                    entry_price       = round(price, 4),
                    stop_loss         = st['stop'],
                    target_1          = st['t1'],
                    target_2          = st['t2'],
                    target_3          = st['t3'],
                    position_size_pct = size,
                    confidence        = confidence,
                    signal_params     = {
                        'cp_prob':    round(cp_prob, 4),
                        'post_mean':  round(post_mean, 6),
                        'hazard':     self.HAZARD_RATE,
                        'vol_annual': round(ticker_vol, 4),
                    },
                ))

        print(f'[debug] {self.id}: signals={len(signals)} '
              f'(long_cands={len(long_cands)}, short_cands={len(short_cands)})', file=sys.stderr)
        return signals
