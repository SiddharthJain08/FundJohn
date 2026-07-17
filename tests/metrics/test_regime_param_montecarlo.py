"""Tests for regime_param_montecarlo — bootstrap CIs for size_scalar."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from metrics import regime_param_montecarlo as mc  # noqa: E402


def test_bootstrap_on_canned_trades_returns_sensible_medians():
    """Half wins of +2%, half losses of -1% → expected mean = 0.5%."""
    pnls = [2.0, -1.0] * 50    # 100 trades, mean=0.5
    result = mc.bootstrap_pnls(pnls, ratio=1.0, n_iter=200, seed=42)
    # Median of bootstrap means should be very close to true mean 0.5
    assert 0.3 < result['mean_pnl_p50'] < 0.7
    # 90% CI should bracket 0.5
    assert result['mean_pnl_p05'] < 0.5 < result['mean_pnl_p95']
    # Sharpe sanity: mean/std with these inputs is positive
    assert result['sharpe_p50'] > 0


def test_bootstrap_scales_linearly_with_ratio():
    pnls = [2.0, -1.0] * 50
    base = mc.bootstrap_pnls(pnls, ratio=1.0, n_iter=200, seed=42)
    half = mc.bootstrap_pnls(pnls, ratio=0.5, n_iter=200, seed=42)
    # Halving the size halves the mean pnl
    assert abs(half['mean_pnl_p50'] - base['mean_pnl_p50'] * 0.5) < 0.1


def test_bootstrap_insufficient_returns_marker():
    result = mc.bootstrap_pnls([1.0, 2.0], ratio=1.0, n_iter=100)
    assert result.get('status') == 'INSUFFICIENT'


def test_bootstrap_zero_iter_returns_empty():
    result = mc.bootstrap_pnls([1.0] * 50, ratio=1.0, n_iter=0)
    assert result.get('status') == 'INSUFFICIENT' or result.get('n_iter') == 0


def test_bootstrap_handles_all_negative():
    """Strategy that lost on every trade — Sharpe should be negative."""
    pnls = [-1.0, -0.5, -2.0] * 30   # 90 trades, all negative
    result = mc.bootstrap_pnls(pnls, ratio=1.0, n_iter=200, seed=42)
    assert result['mean_pnl_p50'] < 0
    assert result['sharpe_p50'] < 0
