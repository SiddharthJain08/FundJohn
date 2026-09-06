"""Spec 2026-09-06 B.2 oracles for q and the CRR American tree (ruling G7)."""
from __future__ import annotations
import itertools
import pytest

from backtest.options_pricing import (bs_price, bs_greeks, american_price, american_delta,
                                      price, delta, AMERICAN_STEPS)


def test_hull_american_put_example():
    # Hull, Options, Futures and Other Derivatives — binomial-tree chapter example:
    # S=50, K=50, r=10 %, σ=40 %, T=5 months. Fine tree ≈ 4.28–4.29; European ≈ 4.08.
    am = american_price('p', 50, 50, 5 / 12, 0.40, r=0.10, steps=500)
    eu = bs_price('p', 50, 50, 5 / 12, 0.40, r=0.10)
    assert 4.25 <= am <= 4.31
    assert eu == pytest.approx(4.08, abs=0.02)
    assert am > eu


def test_american_never_below_european_on_a_grid():
    for flag, K, t, q in itertools.product('cp', (80.0, 100.0, 120.0), (0.1, 0.5), (0.0, 0.03)):
        am = american_price(flag, 100.0, K, t, 0.3, r=0.05, q=q)
        eu = bs_price(flag, 100.0, K, t, 0.3, r=0.05, q=q)
        # an N=200 tree carries ~cent-level discretisation error against the closed form
        assert am >= eu * 0.99 - 0.01, (flag, K, t, q, am, eu)


def test_american_call_without_dividend_is_the_european_call():
    assert american_price('c', 100, 110, 0.5, 0.3, r=0.05) == bs_price('c', 100, 110, 0.5, 0.3, r=0.05)


def test_american_call_with_negative_carry_is_the_european_call_at_q():
    assert american_price('c', 100, 100, 0.5, 0.3, r=0.05, q=-0.02) == bs_price('c', 100, 100, 0.5, 0.3, r=0.05, q=-0.02)


def test_deep_itm_american_put_is_intrinsic():
    assert american_price('p', 20.0, 100.0, 0.5, 0.2, r=0.05) == pytest.approx(80.0, abs=1e-9)


def test_tree_converges():
    p200 = american_price('p', 100, 100, 0.5, 0.3, r=0.05, q=0.02, steps=200)
    p800 = american_price('p', 100, 100, 0.5, 0.3, r=0.05, q=0.02, steps=800)
    assert abs(p200 - p800) / p800 < 0.005
    assert AMERICAN_STEPS == 200


def test_q_lowers_calls_and_raises_puts():
    assert bs_price('c', 100, 100, 0.5, 0.2, r=0.04, q=0.03) < bs_price('c', 100, 100, 0.5, 0.2, r=0.04)
    assert bs_price('p', 100, 100, 0.5, 0.2, r=0.04, q=0.03) > bs_price('p', 100, 100, 0.5, 0.2, r=0.04)
    assert bs_greeks('c', 100, 100, 0.5, 0.2, r=0.04, q=0.03)['delta'] < bs_greeks('c', 100, 100, 0.5, 0.2, r=0.04)['delta']


def test_q_zero_path_is_the_legacy_path():
    # Same py_vollib call as before this task: the 6.627 reference from test_options_pricing.py.
    assert bs_price('c', 100, 100, 0.5, 0.2) == bs_price('c', 100, 100, 0.5, 0.2, q=0.0)
    assert abs(bs_price('c', 100, 100, 0.5, 0.2, q=0.0) - 6.627) < 0.01


def test_dispatchers_and_delta_bounds():
    assert price('p', 100, 100, 0.5, 0.3, r=0.05, exercise='american') == american_price('p', 100, 100, 0.5, 0.3, r=0.05)
    assert price('p', 100, 100, 0.5, 0.3, r=0.05) == bs_price('p', 100, 100, 0.5, 0.3, r=0.05)
    dc = delta('c', 100, 100, 0.5, 0.3, r=0.05, q=0.02, exercise='american')
    dp = delta('p', 100, 100, 0.5, 0.3, r=0.05, q=0.02, exercise='american')
    assert 0.0 < dc < 1.0 and -1.0 < dp < 0.0
    assert american_delta('p', 100, 100, 0.5, 0.3, r=0.05, q=0.02) == dp
    with pytest.raises(ValueError):
        price('p', 100, 100, 0.5, 0.3, exercise='bermudan')
