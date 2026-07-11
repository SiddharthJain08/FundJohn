from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List, Optional
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['PairsTradingJumpDiffusionIntraday']

# Row-block size for the vectorized pair scan. Bounds per-pair temporaries to
# ~block × n_complete float64 arrays (~26 MB per temporary at n≈6300, block=512)
# so a full-width bar stays well under the re-backtest unit's MemoryMax.
_PAIR_BLOCK = 512


def _scan_pairs(recent: np.ndarray, cur_log: np.ndarray, top_k: Optional[int],
                block: int = _PAIR_BLOCK) -> list:
    """Closed-form vectorized replication of the original O(n²) per-pair scan.

    Mathematically equivalent to, for every pair i<j of NaN-free columns of
    ``recent`` (a LOOKBACK×n log-price window whose last row equals ``cur_log``
    on those columns):

        1. OLS  si ~ [sj, 1]  via ``np.linalg.lstsq(rcond=None)`` → beta, alpha
        2. spread = si − beta·sj − alpha
        3. AR(1) spread[1:] ~ [spread[:-1], 1] via lstsq → b, a;
           require 0 < b < 1; kappa = −ln(b)·252; mu_eq = a/(1−b);
           sigma_eq = std(resid, ddof=0)/max(√(1−b²), 1e-8)
        4. keep if sigma_eq ≥ 1e-8 and kappa ≥ 5;
           zscore = (cur_spread − mu_eq)/sigma_eq

    Both regressions are 2-variable OLS, so for full-rank designs the lstsq
    solution equals the closed form from pairwise second moments. All the
    spread moments the AR(1) step needs expand algebraically into entries of
    two Gram matrices over mean-centered data — G = ZᵀZ and the lag-Gram
    L = Z[1:]ᵀZ[:-1] (two BLAS matmuls) — plus boundary rows and column sums:
    with z = x − mean(x) columnwise, d = mu_i − beta·mu_j − alpha (identically
    zero for full-rank designs) and e_t = z_ti − beta·z_tj + d ≡ spread_t,

        Σ_head e      = (Sh_i − β·Sh_j) + N·d
        Σ_head e²     = (G−zLzLᵀ)_ii − 2β(G−zLzLᵀ)_ij + β²(G−zLzLᵀ)_jj + 2d·Σu + Nd²
        Σ e_t e_{t−1} = L_ii − β(L_ij + L_ji) + β²L_jj + d(Σ_head u + Σ_tail u) + Nd²

    (head = rows [0, T−2], tail = rows [1, T−1], N = T−1; tail Grams use the
    first row z0 instead of the last row zL).

    Rank-deficient designs (``lstsq`` returns the MIN-NORM solution there, not
    cov/var):
      * sj exactly constant (= c) over the window — delisted tickers ffilled
        to a constant. Min-norm pinv of [c·1, 1] gives
        beta = c·mean(si)/(c²+1), alpha = mean(si)/(c²+1)  (⇒ spread = si − mean(si)).
      * spread[:-1] exactly constant (= c) in the AR(1) step — happens when
        both columns are constant over rows [0, T−2] (e.g. a halted ticker that
        ticks on the window's last bar). Same min-norm form:
        b = c·ȳ/(c²+1), a = ȳ/(c²+1), with ȳ = mean(spread[1:]); the fitted
        value is then the constant ȳ, so std(resid) = std(spread[1:]).
    Exactness matters: lstsq's rcond cutoff trips whenever the regressor's
    small/large singular-value ratio falls below eps·max(M,N) ≈ 5.6e-14, not
    only on exact rank deficiency — but that band is empty for real panels:
    ffill produces bytewise-identical floats (exactly constant, caught above),
    while genuinely distinct prices differ by at least a tick (≳1e-4), far
    above the cutoff; pinned by tests/test_spairs_vectorized_parity.py.

    Returns candidates as tuples (kappa, i, j, beta, alpha, mu_eq, sigma_eq,
    zscore), stable-sorted by kappa DESC with ties in (i, j) enumeration order
    — exactly the ordering of the original
    ``pair_scores.sort(key=lambda x: x[0], reverse=True)`` — truncated to
    ``top_k`` (None = return all candidates; used by the parity tests).

    Per-block top-k pruning is exact: any global top-k element must be inside
    its own block's stable top-k under the same total order, so the union of
    per-block top-k lists (kept in enumeration order and merged in ascending
    block order) always contains the global top-k.
    """
    T, _n_all = recent.shape
    complete = ~np.isnan(recent).any(axis=0)
    idx = np.flatnonzero(complete)              # enumeration positions of complete cols
    m = int(idx.size)
    if m < 2:
        return []

    X = np.ascontiguousarray(recent[:, idx], dtype=np.float64)
    mu = X.mean(axis=0)
    Z = X - mu                                   # mean-centered (conditioning)
    G = Z.T @ Z                                  # full-window Gram
    L = Z[1:].T @ Z[:-1]                         # lag Gram: L_ab = Σ z_{t,a} z_{t-1,b}
    Gd = np.ascontiguousarray(np.diag(G))
    Ld = np.ascontiguousarray(np.diag(L))
    z0 = Z[0].copy()
    zL = Z[-1].copy()
    sz = Z.sum(axis=0)                           # ≈0; kept for exactness of sub-sums
    Sh = sz - zL                                 # Σ_head z (rows 0..T-2)
    St = sz - z0                                 # Σ_tail z (rows 1..T-1)
    cf = (X == X[0]).all(axis=0)                 # exactly constant over full window
    ch = (X[:-1] == X[0]).all(axis=0)            # exactly constant over head rows
    cval = X[0]                                  # the constant value where cf/ch
    curx = np.asarray(cur_log, dtype=np.float64)[idx]  # == window last row
    N = float(T - 1)

    kept: list[tuple] = []
    for b0 in range(0, m - 1, block):
        b1 = min(b0 + block, m)
        c0 = b0 + 1                              # only j > b0 can pair with this block
        rows = slice(b0, b1)
        cols = slice(c0, m)
        Bi = b1 - b0
        mj = m - c0

        Gij = G[rows, cols]
        Lij = L[rows, cols]
        Lji = L[cols, rows].T                    # L_ji entries, aligned (i, j)

        mu_i = mu[rows][:, None];  mu_j = mu[cols][None, :]
        Gd_i = Gd[rows][:, None];  Gd_j = Gd[cols][None, :]
        Ld_i = Ld[rows][:, None];  Ld_j = Ld[cols][None, :]
        z0_i = z0[rows][:, None];  z0_j = z0[cols][None, :]
        zL_i = zL[rows][:, None];  zL_j = zL[cols][None, :]
        Sh_i = Sh[rows][:, None];  Sh_j = Sh[cols][None, :]
        St_i = St[rows][:, None];  St_j = St[cols][None, :]
        ch_i = ch[rows][:, None];  ch_j = ch[cols][None, :]
        cf_j = cf[cols][None, :]
        cx_i = curx[rows][:, None]; cx_j = curx[cols][None, :]

        with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
            # ── 1st OLS: si on [sj, 1] ─────────────────────────────────────
            beta = Gij / Gd_j                                  # cov/var (centered)
            alpha = mu_i - beta * mu_j
            if cf_j.any():
                cj = cval[cols][None, :]
                den = cj * cj + 1.0
                beta = np.where(cf_j, cj * mu_i / den, beta)   # min-norm pinv
                alpha = np.where(cf_j, mu_i / den, alpha)
            d = mu_i - beta * mu_j - alpha                     # ≡0 on the regular path

            # ── spread moments from Gram algebra ───────────────────────────
            su_h = Sh_i - beta * Sh_j                          # Σ_head (z_i − β z_j)
            su_t = St_i - beta * St_j
            Sx = su_h + N * d                                  # Σ_head spread
            Sy = su_t + N * d                                  # Σ_tail spread
            b2 = beta * beta
            Sxx = ((Gd_i - zL_i * zL_i) - 2.0 * beta * (Gij - zL_i * zL_j)
                   + b2 * (Gd_j - zL_j * zL_j) + 2.0 * d * su_h + N * d * d)
            Syy = ((Gd_i - z0_i * z0_i) - 2.0 * beta * (Gij - z0_i * z0_j)
                   + b2 * (Gd_j - z0_j * z0_j) + 2.0 * d * su_t + N * d * d)
            Sxy = (Ld_i - beta * (Lij + Lji) + b2 * Ld_j
                   + d * (su_h + su_t) + N * d * d)

            # ── AR(1): spread[1:] on [spread[:-1], 1] ──────────────────────
            b = (N * Sxy - Sx * Sy) / (N * Sxx - Sx * Sx)
            a = (Sy - b * Sx) / N
            var_y = Syy / N - (Sy / N) ** 2
            deg = (ch_i & ch_j) | (ch_i & (beta == 0.0))       # spread head exactly const
            if deg.any():
                cpair = (z0_i - beta * z0_j) + d               # spread[0]
                ybar = Sy / N
                dmn = cpair * cpair + 1.0
                b = np.where(deg, cpair * ybar / dmn, b)       # min-norm pinv
                a = np.where(deg, ybar / dmn, a)

            valid = (b > 0.0) & (b < 1.0)
            b_safe = np.where(valid, b, 0.5)
            kappa = -np.log(b_safe) * 252.0
            mu_eq = a / (1.0 - b_safe)
            var_x = Sxx / N - (Sx / N) ** 2
            cov_xy = Sxy / N - (Sx / N) * (Sy / N)
            var_res = var_y - 2.0 * b * cov_xy + b * b * var_x  # Σ(resid−r̄)²/N
            if deg.any():
                var_res = np.where(deg, var_y, var_res)         # fitted ≡ ȳ there
            sigma_eq = (np.sqrt(np.maximum(var_res, 0.0))
                        / np.maximum(np.sqrt(np.maximum(1.0 - b_safe * b_safe, 0.0)), 1e-8))
            valid &= (sigma_eq >= 1e-8) & (kappa >= 5.0)
            # upper triangle: global j > i ⇔ column offset ≥ row offset
            valid &= np.arange(mj)[None, :] >= np.arange(Bi)[:, None]

            cur_spread = (cx_i - beta * cx_j) - alpha
            zscore = (cur_spread - mu_eq) / sigma_eq

        rr, cc = np.nonzero(valid)               # row-major ⇒ (i, j) enumeration order
        if rr.size == 0:
            continue
        kap = kappa[rr, cc]
        if top_k is not None and rr.size > top_k:
            sel = np.argsort(-kap, kind='stable')[:top_k]
            sel.sort()                           # restore enumeration order
            rr, cc, kap = rr[sel], cc[sel], kap[sel]
        kept.append((kap, idx[b0 + rr], idx[c0 + cc], beta[rr, cc], alpha[rr, cc],
                     mu_eq[rr, cc], sigma_eq[rr, cc], zscore[rr, cc]))

    if not kept:
        return []
    kap, ii, jj, be, al, me, se, zz = (np.concatenate(parts) for parts in zip(*kept))
    order = np.argsort(-kap, kind='stable')      # kappa DESC, ties in (i, j) order
    if top_k is not None:
        order = order[:top_k]
    return [(float(kap[o]), int(ii[o]), int(jj[o]), float(be[o]), float(al[o]),
             float(me[o]), float(se[o]), float(zz[o])) for o in order]


class PairsTradingJumpDiffusionIntraday(BaseStrategy):
    """OU+jump-diffusion pairs: rank by mean-reversion speed kappa, trade z-score entry/exit thresholds."""

    id          = 'S_pairs_trading_jump_diffusion_intraday'
    name        = 'PairsTradingJumpDiffusionIntraday'
    description = 'OU+jump-diffusion pairs: rank by kappa, trade individualized z-score entry/exit thresholds'
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    LOOKBACK  = 252
    TOP_K     = 5
    ENTRY_Z   = 1.5
    BASE_SIZE = 0.04   # fraction per leg

    def _fit_ou(self, spread: np.ndarray):
        """Fit AR(1) to spread; return (kappa_annual, mu_eq, sigma_eq) or None.

        Kept for API compatibility; the vectorized ``_scan_pairs`` closed form
        replicates this per-pair math (see its docstring)."""
        y, x = spread[1:], spread[:-1]
        X = np.column_stack([x, np.ones(len(x))])
        coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        b, a = float(coeffs[0]), float(coeffs[1])
        if not (0.0 < b < 1.0):
            return None
        kappa = -np.log(b) * 252.0
        mu_eq = a / (1.0 - b)
        resid = y - (a + b * x)
        sigma_eq = float(np.std(resid)) / max(np.sqrt(1.0 - b ** 2), 1e-8)
        return float(kappa), float(mu_eq), sigma_eq

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        available = [t for t in universe if t in prices.columns]
        if len(available) < 4 or len(prices) < self.LOOKBACK:
            print(f'[debug] signals=0 (n={len(available)}, rows={len(prices)})', file=sys.stderr)
            return []

        log_px = np.log(prices[available].ffill().dropna(how='all'))
        if len(log_px) < self.LOOKBACK:
            print(f'[debug] signals=0 (log dropna reduced rows below lookback)', file=sys.stderr)
            return []

        recent = log_px.iloc[-self.LOOKBACK:].values
        cur_log = log_px.iloc[-1].values
        cur_px  = prices[available].iloc[-1]
        tickers = list(log_px.columns)

        # Vectorized closed-form pair scan — mathematically equivalent to the
        # original per-pair double-lstsq loop (see _scan_pairs docstring).
        pair_scores = [
            (kappa, tickers[i], tickers[j], beta, alpha, mu_eq, sigma_eq, zscore)
            for kappa, i, j, beta, alpha, mu_eq, sigma_eq, zscore
            in _scan_pairs(recent, cur_log, self.TOP_K)
        ]
        signals: List[Signal] = []

        for kappa, ti, tj, beta, alpha, mu_eq, sigma_eq, zscore in pair_scores[:self.TOP_K]:
            # Individualized threshold — higher kappa → tighter entry
            entry_z = max(1.0, self.ENTRY_Z - kappa / 500.0)
            if abs(zscore) < entry_z:
                continue
            pi = float(cur_px[ti])
            pj = float(cur_px[tj])
            if not (np.isfinite(pi) and pi > 0.0 and np.isfinite(pj) and pj > 0.0):
                continue
            dir_i = 'LONG' if zscore < -entry_z else 'SHORT'
            dir_j  = 'SHORT' if dir_i == 'LONG' else 'LONG'
            conf   = 'HIGH' if abs(zscore) > 2.0 else 'MED'
            size   = round(float(self.BASE_SIZE * scale), 4)
            params = {
                'pair':   f'{ti}/{tj}',
                'zscore': round(float(zscore), 4),
                'kappa':  round(float(kappa), 4),
                'beta':   round(float(beta), 4),
                'entry_z': round(float(entry_z), 4),
            }
            st_i = self.compute_stops_and_targets(
                prices[ti].dropna(), dir_i, pi, regime_state=regime_state
            )
            st_j = self.compute_stops_and_targets(
                prices[tj].dropna(), dir_j, pj, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ti, direction=dir_i, entry_price=pi,
                stop_loss=st_i['stop'], target_1=st_i['t1'], target_2=st_i['t2'], target_3=st_i['t3'],
                position_size_pct=size, confidence=conf, signal_params=params,
            ))
            signals.append(Signal(
                ticker=tj, direction=dir_j, entry_price=pj,
                stop_loss=st_j['stop'], target_1=st_j['t1'], target_2=st_j['t2'], target_3=st_j['t3'],
                position_size_pct=size, confidence=conf, signal_params={**params, 'zscore': round(-float(zscore), 4)},
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
