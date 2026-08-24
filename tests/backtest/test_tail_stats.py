"""tail_stats.py — advisory sleeve tail statistics (task P3+R3, 2026-08-24).

Pure numpy, no DB. Hand-computed cases per the task brief:
  - 6-value case with alpha/min_obs overridden so both sortino and cvar_5
    resolve to concrete (non-None) numbers.
  - all-positive returns -> zero downside_dev -> sortino None; cvar_5 is the
    mean of the single worst (smallest) observation.
  - n below min_obs -> everything None.
  - n==20 boundary with the default alpha=0.05 -> floor(0.05*20)==1 exactly
    (not 0 from a floating-point short-fall), so cvar_5 is a live number.

Expected values below are derived independently from the documented formula
(plain Python arithmetic / math.sqrt), not by calling sleeve_tail_stats.
"""
from __future__ import annotations

import math

import pytest

from backtest.tail_stats import sleeve_tail_stats


def test_six_value_hand_computed():
    # mean = (6+7+7-2-2-4)/6 = 12/6 = 2.0
    # downside sq sum = 2^2+2^2+4^2 = 4+4+16 = 24; /6 = 4.0; sqrt = 2.0
    # sortino = 2.0/2.0 = 1.0
    # alpha=0.2 -> floor(0.2*6)=floor(1.2)=1 -> cvar_5 = worst single value = -4.0
    r = [6, 7, 7, -2, -2, -4]
    out = sleeve_tail_stats(r, alpha=0.2, min_obs=6)
    assert out['downside_dev'] == pytest.approx(2.0)
    assert out['sortino'] == pytest.approx(1.0)
    assert out['cvar_5'] == pytest.approx(-4.0)


def test_all_positive_returns_sortino_none_cvar_is_least_value():
    # n=20, all positive -> min(r,0) is 0 everywhere -> downside_dev == 0
    # -> sortino must be None per spec even though mean > 0.
    # alpha=0.05 (default) -> floor(0.05*20)=1 -> cvar_5 = mean of the worst
    # (smallest) single observation = 1.0.
    r = list(range(1, 21))  # 1..20, all positive
    out = sleeve_tail_stats(r)
    assert out['downside_dev'] == pytest.approx(0.0)
    assert out['sortino'] is None
    assert out['cvar_5'] == pytest.approx(1.0)


def test_below_min_obs_all_none():
    r = [1, -2, 3, -4, 5, -6, 7, -8, 9, -10]  # n=10 < default min_obs=20
    out = sleeve_tail_stats(r)
    assert out == {'sortino': None, 'cvar_5': None, 'downside_dev': None}


def test_alpha_edge_n20_floor_is_one_not_zero():
    # 19 values of +2.0 and one value of -20.0 -> n=20 exactly at min_obs.
    # floor(0.05*20) must land on 1, not 0 from float short-fall, so cvar_5
    # resolves to the single worst value (-20.0), not None.
    r = [2.0] * 19 + [-20.0]
    out = sleeve_tail_stats(r)  # default alpha=0.05, min_obs=20
    assert out['cvar_5'] == pytest.approx(-20.0)

    expected_mean = (19 * 2.0 + (-20.0)) / 20.0
    expected_downside_dev = math.sqrt(((-20.0) ** 2) / 20.0)
    expected_sortino = expected_mean / expected_downside_dev
    assert out['downside_dev'] == pytest.approx(expected_downside_dev)
    assert out['sortino'] == pytest.approx(expected_sortino)


def test_alpha_zero_floor_zero_cvar_none():
    # floor(alpha*n) == 0 -> cvar_5 must be None (documented boundary).
    r = [1, -2, 3, -4, 5, -6, 7, -8, 9, -10] * 3  # n=30, mixed
    out = sleeve_tail_stats(r, alpha=0.01)  # floor(0.01*30)=floor(0.3)=0
    assert out['cvar_5'] is None
    # sortino/downside_dev are unaffected by alpha and should still resolve.
    assert out['sortino'] is not None
    assert out['downside_dev'] is not None and out['downside_dev'] > 0


def test_return_type_is_plain_python_floats_or_none():
    r = [6, 7, 7, -2, -2, -4]
    out = sleeve_tail_stats(r, alpha=0.2, min_obs=6)
    for key in ('sortino', 'cvar_5', 'downside_dev'):
        assert out[key] is None or isinstance(out[key], float)
