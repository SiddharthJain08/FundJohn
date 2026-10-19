"""Exact OU discretisation in S_pca_etf_stat_arb_reversion (spec B2)."""
import importlib

import numpy as np
import pytest

_ou_fit_exact = importlib.import_module(
    'strategies.implementations.S_pca_etf_stat_arb_reversion')._ou_fit_exact

PHIS = (0.5, 0.8, 0.9, 0.95)


def test_exact_recovery_known_kappa():
    rng = np.random.default_rng(3)
    n, mean = 40_000, 0.3
    X = np.zeros((n, len(PHIS)))
    for j, phi in enumerate(PHIS):
        e = rng.normal(size=n)
        x = np.zeros(n)
        for t in range(1, n):
            x[t] = phi * x[t - 1] + e[t]
        X[:, j] = x + mean
    kappa, m, sigma_eq = _ou_fit_exact(X)
    for j, phi in enumerate(PHIS):
        assert kappa[j] == pytest.approx(-np.log(phi), rel=0.05)
        assert sigma_eq[j] == pytest.approx(1 / np.sqrt(1 - phi ** 2), rel=0.03)
        assert abs(m[j] - mean) < 0.1 * sigma_eq[j]


def test_explosive_series_is_nan():
    x = 1.02 ** np.arange(300, dtype=float) + np.random.default_rng(1).normal(size=300) * 0.01
    kappa, m, sigma_eq = _ou_fit_exact(x.reshape(-1, 1))
    assert np.isnan(kappa[0]) and np.isnan(sigma_eq[0])
