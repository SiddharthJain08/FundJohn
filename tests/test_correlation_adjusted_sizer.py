"""Tests for correlation_adjusted_sizer — portfolio-Kelly downsize."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import correlation_adjusted_sizer as ca  # noqa: E402


def test_identity_no_correlation_returns_phi_one():
    """When all off-diagonal correlations are 0 (perfect independence),
    phi should be exactly 1.0."""
    orders = [
        {'ticker': 'AAPL', 'qty': 100, 'notional_usd': 15000, 'direction': 'LONG'},
        {'ticker': 'MSFT', 'qty': 50, 'notional_usd': 15000, 'direction': 'LONG'},
    ]
    sigma = {
        'AAPL': {'AAPL': 1.0, 'MSFT': 0.0},
        'MSFT': {'AAPL': 0.0, 'MSFT': 1.0},
    }
    adjusted, log = ca.adjust(orders, sigma)
    for entry in log:
        assert abs(entry['phi'] - 1.0) < 0.001


def test_fully_correlated_long_only_downsizes():
    """Two perfectly positively correlated LONG positions of equal size
    → phi = 1/sqrt(2) ≈ 0.707."""
    orders = [
        {'ticker': 'A', 'qty': 100, 'notional_usd': 10000, 'direction': 'LONG'},
        {'ticker': 'B', 'qty': 100, 'notional_usd': 10000, 'direction': 'LONG'},
    ]
    # Use 0.95 (the clamp) instead of 1.0 to stay within MAX_OFF_DIAGONAL.
    sigma = {
        'A': {'A': 1.0, 'B': 0.95},
        'B': {'A': 0.95, 'B': 1.0},
    }
    adjusted, log = ca.adjust(orders, sigma)
    # phi should be < 1 (downsize); for 2 equal correlated LONGs, phi = sqrt(2 / (2*(1+rho)))
    # With rho=0.95: phi = sqrt(2/3.9) ≈ 0.716
    expected_phi = (2 / (2 * (1 + 0.95))) ** 0.5
    for entry in log:
        assert abs(entry['phi'] - expected_phi) < 0.05


def test_offsetting_longs_shorts_no_downsize():
    """Long and short of correlated tickers cancel risk → phi = 1.0
    (clamp prevents upsize)."""
    orders = [
        {'ticker': 'A', 'qty': 100, 'notional_usd': 10000, 'direction': 'LONG'},
        {'ticker': 'B', 'qty': 100, 'notional_usd': 10000, 'direction': 'SHORT'},
    ]
    sigma = {
        'A': {'A': 1.0, 'B': 0.95},
        'B': {'A': 0.95, 'B': 1.0},
    }
    adjusted, log = ca.adjust(orders, sigma)
    # σ_p² for correlated long/short:
    #   w = [+10000, -10000], σ_p² = 10000² + 10000² + 2·(-10000²)·0.95 = (2 - 1.9)·10000²
    #     = 0.1 · 10000² (much smaller than independent variance 2·10000²)
    # So sqrt(σ_ind² / σ_p²) is HUGE > 1 → clamp at 1.0.
    for entry in log:
        assert entry['phi'] == 1.0


def test_clamp_at_one_never_upsizes():
    """Even when correlations are net-negative (would suggest upsizing),
    phi clamps at 1.0."""
    orders = [
        {'ticker': 'A', 'qty': 100, 'notional_usd': 10000, 'direction': 'LONG'},
        {'ticker': 'B', 'qty': 100, 'notional_usd': 10000, 'direction': 'LONG'},
    ]
    # Negative off-diagonal → portfolio less risky than independent
    sigma = {
        'A': {'A': 1.0, 'B': -0.5},
        'B': {'A': -0.5, 'B': 1.0},
    }
    adjusted, log = ca.adjust(orders, sigma)
    for entry in log:
        assert entry['phi'] == 1.0


def test_empty_orders_returns_empty():
    adjusted, log = ca.adjust([], {})
    assert adjusted == []
    assert log == []


def test_single_order_unchanged():
    """Only one position → no portfolio interaction → phi=1.0."""
    orders = [{'ticker': 'A', 'qty': 100, 'notional_usd': 10000, 'direction': 'LONG'}]
    sigma = {'A': {'A': 1.0}}
    adjusted, log = ca.adjust(orders, sigma)
    assert log[0]['phi'] == 1.0
    assert adjusted[0]['notional_usd'] == 10000
    assert adjusted[0]['qty'] == 100


def test_top_n_cap_excludes_small_orders():
    """Phase 2G-v2: only the top-N orders by |notional| participate in
    the correlation matrix; smaller orders pass through with phi=1.0."""
    # Two big LONG orders (correlated → phi < 1) + a tiny LONG that
    # would also be correlated; with top_n=2 the tiny order bypasses.
    orders = [
        {'ticker': 'A', 'qty': 100, 'notional_usd': 50000, 'direction': 'LONG'},
        {'ticker': 'B', 'qty': 100, 'notional_usd': 50000, 'direction': 'LONG'},
        {'ticker': 'C', 'qty': 1,   'notional_usd': 100,   'direction': 'LONG'},  # tiny
    ]
    sigma = {
        'A': {'A': 1.0, 'B': 0.95, 'C': 0.95},
        'B': {'A': 0.95, 'B': 1.0, 'C': 0.95},
        'C': {'A': 0.95, 'B': 0.95, 'C': 1.0},
    }
    adjusted, log = ca.adjust(orders, sigma, top_n=2)
    by_ticker = {entry['ticker']: entry for entry in log}
    # A and B are in top-2 → phi < 1
    assert by_ticker['A']['phi'] < 1.0
    assert by_ticker['B']['phi'] < 1.0
    assert by_ticker['A']['in_top_n'] is True
    assert by_ticker['B']['in_top_n'] is True
    # C is out → phi = 1.0, notional unchanged
    assert by_ticker['C']['phi'] == 1.0
    assert by_ticker['C']['in_top_n'] is False
    c_adj = next(a for a in adjusted if a['ticker'] == 'C')
    assert c_adj['notional_usd'] == 100
    assert c_adj['qty'] == 1
