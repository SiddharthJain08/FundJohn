"""Convex portfolio helpers (Clarabel via cvxpy) for tangency and min-EVaR weights.

cvxpy is imported lazily inside each function (import costs ~0.8 s). The solver
is always passed explicitly. Strategy files must NOT import cvxpy; they call
these helpers via ``from lib.convex import ...`` and fall back on ``None``.
"""
from __future__ import annotations

import numpy as np

_OK = ('optimal', 'optimal_inaccurate')


def _clip_norm(x: np.ndarray) -> np.ndarray | None:
    x = np.maximum(np.asarray(x, dtype=float).ravel(), 0.0)
    tot = x.sum()
    if not np.isfinite(tot) or tot <= 1e-12:
        return None
    return x / tot


def tangency_weights(mu: np.ndarray, Sigma: np.ndarray,
                     long_only: bool = True) -> np.ndarray | None:
    """Max-Sharpe (tangency) weights summing to 1, or None when unsolvable.

    long_only: min y'Σy s.t. mu'y = 1, y >= 0; p = y / 1'y.
    otherwise: closed form Σ^-1 mu / 1'Σ^-1 mu.
    """
    mu = np.asarray(mu, dtype=float).ravel()
    Sigma = np.asarray(Sigma, dtype=float)
    if not long_only:
        try:
            x = np.linalg.solve(Sigma, mu)
        except np.linalg.LinAlgError:
            return None
        den = float(x.sum())
        if not np.isfinite(den) or abs(den) < 1e-12:
            return None
        return x / den
    if not np.all(np.isfinite(mu)) or not np.any(mu > 0):
        return None
    import cvxpy as cp
    n = mu.shape[0]
    if Sigma.shape != (n, n) or not np.all(np.isfinite(Sigma)):
        return None
    Sigma = (Sigma + Sigma.T) / 2.0
    # psd_wrap would hide a non-PSD input from cvxpy's check: refuse it instead
    if float(np.linalg.eigvalsh(Sigma).min()) < -1e-10:
        return None
    y = cp.Variable(n)
    prob = cp.Problem(cp.Minimize(cp.quad_form(y, cp.psd_wrap(Sigma))),
                      [mu @ y == 1, y >= 0])
    try:
        prob.solve(solver=cp.CLARABEL)
    except Exception:
        return None
    if prob.status not in _OK or y.value is None:
        return None
    return _clip_norm(y.value)


def min_evar_weights(L: np.ndarray, z: float = 0.95) -> np.ndarray | None:
    """Long-only minimum-EVaR weights from a TxN matrix of scenario LOSSES.

    EVaR_z(l) = inf_{t>0} (1/t)(log mean e^{t l} - log(1-z)); with s = 1/t the
    perspective of log-sum-exp is one exponential-cone program (log-mean form).
    """
    L = np.asarray(L, dtype=float)
    if L.ndim != 2 or not np.all(np.isfinite(L)):
        return None
    import cvxpy as cp
    T, n = L.shape
    w = cp.Variable(n, nonneg=True)
    eta = cp.Variable()
    s = cp.Variable(nonneg=True)
    u = cp.Variable(T)
    cons = [cp.sum(w) == 1,
            cp.constraints.ExpCone(L @ w - eta, s * np.ones(T), u),
            cp.sum(u) <= T * s]
    prob = cp.Problem(cp.Minimize(eta - s * np.log(1.0 - z)), cons)
    try:
        prob.solve(solver=cp.CLARABEL)
    except Exception:
        return None
    if prob.status not in _OK or w.value is None:
        return None
    return _clip_norm(w.value)
