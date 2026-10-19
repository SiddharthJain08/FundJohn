"""tests/lib/test_convex.py — B5 convex helpers (tangency, min-EVaR)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import minimize, minimize_scalar

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib.convex import min_evar_weights, tangency_weights  # noqa: E402


def test_tangency_uncorrelated_analytic():
    Sigma = np.diag([0.04, 0.09, 0.01])
    mu = np.array([0.05, 0.06, 0.02])
    x = mu / np.diag(Sigma)
    p = tangency_weights(mu, Sigma)
    assert np.allclose(p, x / x.sum(), atol=1e-6)


def _sharpe(p, mu, cov):
    return float(p @ mu) / float(np.sqrt(p @ cov @ p))


@pytest.mark.parametrize('seed', range(5))
def test_tangency_beats_or_ties_slsqp(seed):
    rng = np.random.default_rng(seed)
    n = int(rng.integers(4, 9))
    A = rng.normal(size=(n, n))
    cov = A @ A.T / n + 0.01 * np.eye(n)
    mu = rng.normal(0.02, 0.03, n)
    mu[0] = abs(mu[0]) + 0.01

    def neg_sharpe(p):
        v = float(np.sqrt(p @ cov @ p + 1e-12))
        return -float(p @ mu) / v if v > 0 else 0.0

    res = minimize(neg_sharpe, np.ones(n) / n, method='SLSQP',
                   bounds=[(0, 1)] * n,
                   constraints=[{'type': 'eq', 'fun': lambda p: p.sum() - 1.0}],
                   options={'ftol': 1e-9, 'maxiter': 500})
    p = tangency_weights(mu, cov)
    assert p is not None and p.min() >= 0 and p.sum() == pytest.approx(1)
    assert _sharpe(p, mu, cov) >= _sharpe(res.x, mu, cov) - 1e-6


def test_tangency_nonpositive_mu_returns_none():
    assert tangency_weights(np.array([-0.01, 0.0]), np.eye(2)) is None


def test_tangency_unconstrained_closed_form():
    Sigma = np.array([[0.04, 0.01], [0.01, 0.09]])
    mu = np.array([0.05, 0.06])
    x = np.linalg.solve(Sigma, mu)
    p = tangency_weights(mu, Sigma, long_only=False)
    assert np.allclose(p, x / x.sum())


def _evar(losses, z=0.95):
    log_1mz = np.log(1.0 - z)

    def obj(lt):
        t = np.exp(lt)
        m = float(np.mean(np.exp(np.clip(t * losses, -50.0, 50.0))))
        return (np.log(m) - log_1mz) / t

    return float(minimize_scalar(obj, bounds=(-9, 9), method='bounded').fun)


def _ref_min_evar(log_rets, z=0.95):
    # in-test copy of S_evar_tempered_stable_sector_etf._min_evar_weights
    n = log_rets.shape[1]
    w0 = np.full(n, 1.0 / n)
    res = minimize(lambda w: _evar(-(log_rets @ w), z), w0, method='SLSQP',
                   bounds=[(0.0, 1.0)] * n,
                   constraints=[{'type': 'eq', 'fun': lambda w: w.sum() - 1.0}],
                   options={'maxiter': 80, 'ftol': 1e-6})
    w = res.x if (res.success and np.all(np.isfinite(res.x))) else w0
    w = np.maximum(w, 0.0)
    return w / w.sum()


def test_min_evar_beats_or_ties_slsqp():
    R = np.random.default_rng(9).normal(0.0005, 0.01, (250, 5))
    L = -R
    w_cvx = min_evar_weights(L)
    w_ref = _ref_min_evar(R)
    assert w_cvx is not None
    assert _evar(L @ w_cvx) <= _evar(L @ w_ref) + 1e-5
    assert w_cvx.min() >= -1e-9
    assert w_cvx.sum() == pytest.approx(1)


def test_requirements_pin_cvxpy_and_clarabel():
    lines = (ROOT / 'requirements.txt').read_text().splitlines()
    assert 'cvxpy==1.8.2' in lines and 'clarabel==0.11.1' in lines


def test_helpers_lint_clean_for_strategies():
    from strategies.strategy_lint import lint_source
    assert lint_source('from lib.convex import tangency_weights\n') == []


def test_tangency_rejects_non_psd_sigma():
    Sigma = np.array([[1.0, 2.0], [2.0, 1.0]])   # eigenvalues 3, -1
    assert tangency_weights(np.array([0.1, 0.2]), Sigma) is None


def test_tangency_symmetrises_sigma():
    S = np.array([[0.04, 0.0100], [0.0102, 0.09]])
    a = tangency_weights(np.array([0.1, 0.2]), S)
    b = tangency_weights(np.array([0.1, 0.2]), (S + S.T) / 2)
    assert a is not None and np.allclose(a, b, atol=1e-6)
