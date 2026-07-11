from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from scipy.special import poch as _poch
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['BOCPDChangePoint']

# ─── Vectorization notes (2026-07-11, §7 re-backtest unblock) ────────────────
# The original implementation ran the 126-step BOCPD recursion PER TICKER and
# called scipy.stats.t.logpdf once per step (~100µs of rv_continuous dispatch
# per call → ~800k scipy calls per full-width bar → the §7 re-backtest blew a
# 25-hour watchdog). Two mathematically-neutral changes fix it:
#   1. `_t_logpdf` — pure-numpy Student-t logpdf that mirrors scipy 1.15's
#      `t._logpdf` arithmetic exactly (log∘poch normalization constant,
#      log1p kernel), so per-element results match stats.t.logpdf bitwise.
#   2. `_bocpd_panel` — ONE recursion over a (T × m) panel of return series
#      instead of m scalar recursions. The recursion stays sequential in t;
#      all per-run-length work is vectorized across tickers. This is exact
#      because n_obs[r] == r at every step (the sufficient-statistic shift
#      starts at zero and increments), so dof = 2·(alpha0 + r/2) is
#      deterministic, shared by every ticker, and precomputable.
# `_bocpd` keeps its original signature as a thin wrapper over the panel
# version. Parity is pinned by tests/test_tr03_bocpd_vectorized_parity.py
# against a frozen copy of the original implementation.


def _nig_params(sums: np.ndarray, sq_sums: np.ndarray, n_obs: np.ndarray,
                mu0: float, kappa0: float, alpha0: float, beta0: float):
    """Return NIG posterior parameters for each run length.

    Shape-polymorphic: works on 1-D (t+1,) vectors (original scalar path) and
    on (t+1, m) panels with n_obs shaped (t+1, 1) (vectorized path) — every
    op is elementwise/broadcast, so per-element arithmetic is identical."""
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


def _t_logpdf_const(dof: np.ndarray) -> np.ndarray:
    """Student-t log-normalization constant.

    Mirrors scipy 1.15 `t._logpdf` exactly:
        log(poch(df/2, 1/2)) − 0.5·(log(df) + log(π))
    log∘poch (= gammaln((df+1)/2) − gammaln(df/2)) is kept in this exact form
    so the constant matches scipy's bitwise. In the recursion dof depends only
    on the run-length index, so this is precomputed ONCE per panel call."""
    dof = np.asarray(dof, dtype=np.float64)
    return np.log(_poch(0.5 * dof, 0.5)) - 0.5 * (np.log(dof) + np.log(np.pi))


def _t_logpdf(x, dof, loc, scale) -> np.ndarray:
    """Pure-numpy Student-t logpdf.

    Elementwise-identical to `scipy.stats.t.logpdf(x, df=dof, loc=loc,
    scale=scale)` for finite inputs and scale > 0 (same operations, same
    association order), without the rv_continuous dispatch overhead."""
    x     = np.asarray(x, dtype=np.float64)
    dof   = np.asarray(dof, dtype=np.float64)
    scale = np.asarray(scale, dtype=np.float64)
    z = (x - loc) / scale
    return (_t_logpdf_const(dof)
            - (dof + 1) / 2 * np.log1p(z * z / dof)) - np.log(scale)


def _bocpd_panel(returns_panel: np.ndarray, hazard_rate: float = 0.005
                 ) -> tuple[np.ndarray, np.ndarray]:
    """
    Bayesian Online Change Point Detection (Adams & MacKay 2007), vectorized
    across tickers. Normal-Inverse-Gamma conjugate model → Student-t predictive.

    Runs ONE recursion over a (T, m) panel — mathematically identical to
    running the original scalar recursion on each column independently:

        R_t[0]   ∝ H × pred_prior(x_t)          ← prior predictive
        R_t[r+1] ∝ (1-H) × pred_r(x_t) × R_{t-1}[r]

    −inf bookkeeping mirrors the scalar code: entries beyond run length t+1
    stay −inf; exp(−inf − max) ≡ 0.0 drops masked entries from the
    log-sum-exp normalization exactly like the original finite mask.
    (Degenerate all-−inf columns can only arise from non-finite inputs, where
    the original raised; real return panels always leave R_t[0] finite.)

    Args:
        returns_panel — (T, m) float array, no NaNs (callers pre-extract
                        each ticker's non-NaN return window).
    Returns:
        cp_probs  — P(CP at t | x_{1:t}) per ticker, shape (T, m)
        run_dist  — final posterior run-length distribution, shape (T+1, m)
    """
    rp = np.asarray(returns_panel, dtype=np.float64)
    if rp.ndim != 2:
        raise ValueError(f'_bocpd_panel expects a 2-D (T, m) panel, got ndim={rp.ndim}')
    T, m = rp.shape
    if T < 10:
        return np.zeros((T, m)), np.zeros((T + 1, m))

    mu0, kappa0, alpha0, beta0 = 0.0, 1.0, 1.0, 0.01
    log_h   = np.log(hazard_rate)
    log_1mh = np.log(1.0 - hazard_rate)

    # Run-length invariant: at the top of step t, n_obs[r] == r for r ∈ [0, t]
    # (starts at zero, shift-increments each step). A precomputed index column
    # therefore replaces the scalar version's mutable n_obs array…
    n_col = np.arange(T + 1, dtype=np.int32)[:, None]                 # (T+1, 1)
    # …and dof = 2·(alpha0 + r/2) is deterministic and ticker-independent, so
    # the Student-t constants are precomputed once instead of T×m gammaln
    # evaluations behind scipy dispatch.
    dof_r    = 2.0 * (alpha0 + np.arange(T, dtype=np.float64) * 0.5)  # (T,)
    tconst   = _t_logpdf_const(dof_r)[:, None]                        # (T, 1)
    half_dp1 = ((dof_r + 1) / 2)[:, None]                             # (T, 1)

    # Work in log space for numerical stability
    log_R       = np.full((T + 1, m), -np.inf)
    log_R[0, :] = 0.0  # log(1)
    sums        = np.zeros((T + 1, m))
    sq_sums     = np.zeros((T + 1, m))
    cp_probs    = np.zeros((T, m))

    for t in range(T):
        x  = rp[t, :]                                                 # (m,)
        sl = slice(0, t + 1)

        mu_r, scale, dof = _nig_params(
            sums[sl], sq_sums[sl], n_col[sl], mu0, kappa0, alpha0, beta0
        )                                            # (t+1, m); dof (t+1, 1)

        # Log predictive for each (run length, ticker) — same elementwise
        # arithmetic as _t_logpdf / stats.t.logpdf, constants presliced.
        z = (x[np.newaxis, :] - mu_r) / scale
        log_pred = (tconst[sl]
                    - half_dp1[sl] * np.log1p(z * z / dof)) - np.log(scale)

        # Adams & MacKay eq. 3:
        #   log P_new[0]   = log(H)   + log_pred[0]               ← prior predictive
        #   log P_new[r+1] = log(1-H) + log_pred[r] + log_R[r]    ← continue
        prev      = log_R[sl]
        log_R_new = np.empty((t + 2, m))
        log_R_new[0, :]  = log_h + log_pred[0, :]
        log_R_new[1:, :] = np.where(prev > -np.inf,
                                    log_1mh + log_pred + prev, -np.inf)

        # Normalise in log space (log-sum-exp); exp(−inf − max) = 0 excludes
        # dead run lengths from the sum exactly like the scalar finite mask.
        max_val    = log_R_new.max(axis=0)                            # (m,)
        log_norm   = max_val + np.log(
            np.exp(log_R_new - max_val[np.newaxis, :]).sum(axis=0))
        log_R_new -= log_norm[np.newaxis, :]

        log_R[:t + 2] = log_R_new       # rows > t+1 remain −inf
        cp_probs[t]   = np.exp(log_R_new[0])

        # Shift sufficient statistics right (row 0 stays the empty-run prior)
        sums[1: t + 2]    = sums[sl] + x
        sq_sums[1: t + 2] = sq_sums[sl] + x ** 2
        sums[0, :]    = 0.0
        sq_sums[0, :] = 0.0

    return cp_probs, np.exp(log_R)


def _bocpd(returns: np.ndarray, hazard_rate: float = 0.005) -> tuple[np.ndarray, np.ndarray]:
    """
    Bayesian Online Change Point Detection (Adams & MacKay 2007) for ONE
    return series — original public signature, now a thin wrapper over
    `_bocpd_panel` (identical output).

    Returns:
        cp_probs  — P(CP at t | x_{1:t}) = R_t[0], shape (T,)
        run_dist  — final posterior run-length distribution, shape (T+1,)
    """
    arr = np.asarray(returns, dtype=np.float64)
    cp, R = _bocpd_panel(arr[:, None], hazard_rate=hazard_rate)
    return np.ascontiguousarray(cp[:, 0]), np.ascontiguousarray(R[:, 0])


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

    BOCPD_WINDOW  = 126   # bars fed to BOCPD per ticker (== min_lookback; the
                          # panel fast path needs min_lookback >= BOCPD_WINDOW so
                          # every eligible ticker contributes EXACTLY this many returns)
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

        # ── Vectorized BOCPD: one panel recursion instead of a per-ticker
        #    scipy loop. Eligibility mirrors the old per-ticker check:
        #    len(returns_df[t].dropna()) >= min_lookback. ──────────────────
        W        = self.BOCPD_WINDOW
        ret_np   = returns_df.to_numpy(dtype=np.float64, copy=False)
        nan_mask = np.isnan(ret_np)
        n_valid  = (~nan_mask).sum(axis=0)
        elig_idx = np.flatnonzero(n_valid >= self.min_lookback)

        if elig_idx.size and self.min_lookback >= W:
            # prices were ffill'd above, so NaNs in returns are leading-only
            # on real panels → an eligible column's last W rows ARE its last
            # W non-NaN returns (n_valid >= min_lookback >= W puts the tail
            # inside the finite run). Columns violating that (e.g. interior
            # NaN from a 0/0 pct_change on a died-to-zero price) fall back to
            # the exact per-ticker extraction the old code used: dropna → tail.
            panel = ret_np[-W:, elig_idx]              # fancy index → fresh array
            dirty = nan_mask[-W:, elig_idx].any(axis=0)
            for pos in np.flatnonzero(dirty):
                j = elig_idx[pos]
                panel[:, pos] = ret_np[:, j][~nan_mask[:, j]][-W:]

            cp_panel, R_panel = _bocpd_panel(panel, hazard_rate=self.HAZARD_RATE)
            recent_cp_panel   = cp_panel[-self.CP_LOOKBACK:, :].max(axis=0)

            for pos in range(elig_idx.size):
                ticker = tickers[elig_idx[pos]]

                # Recent CP: max probability in last CP_LOOKBACK bars
                recent_cp = float(recent_cp_panel[pos])
                if recent_cp < self.CP_THRESHOLD:
                    continue

                # Direction: most likely current run length → mean of post-break returns
                most_likely_rl = int(R_panel[:, pos].argmax())
                if most_likely_rl == 0:
                    most_likely_rl = 1
                arr        = np.ascontiguousarray(panel[:, pos])
                post_break = arr[-most_likely_rl:]
                post_mean  = float(post_break.mean()) if len(post_break) > 0 else 0.0

                if post_mean > 0:
                    long_cands.append((ticker, recent_cp, post_mean))
                elif post_mean < 0:
                    short_cands.append((ticker, recent_cp, post_mean))
        elif elig_idx.size:
            # Defensive slow path: only reachable if min_lookback is ever
            # edited below BOCPD_WINDOW (per-ticker windows would then have
            # ragged lengths — a uniform panel would be silently wrong).
            for j in elig_idx:
                series = ret_np[:, j][~nan_mask[:, j]]
                arr    = series[-W:]
                cp_probs, R = _bocpd(arr, hazard_rate=self.HAZARD_RATE)
                recent_cp = float(cp_probs[-self.CP_LOOKBACK:].max())
                if recent_cp < self.CP_THRESHOLD:
                    continue
                most_likely_rl = int(R.argmax())
                if most_likely_rl == 0:
                    most_likely_rl = 1
                post_break = arr[-most_likely_rl:]
                post_mean  = float(post_break.mean()) if len(post_break) > 0 else 0.0
                ticker = tickers[j]
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
