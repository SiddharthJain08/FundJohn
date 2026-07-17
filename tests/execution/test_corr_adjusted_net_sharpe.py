"""Pure-function tests for the correlation-adjusted cumulative-Sharpe gate."""
from __future__ import annotations
import math, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import pytest  # noqa: E402
from execution.orthogonalization import corr_adjusted_net_sharpe  # noqa: E402


def _sim(sids, rho=0.0):
    """Similarity matrix: diagonal 1.0, all off-diagonal = rho."""
    m = {a: {} for a in sids}
    for a in sids:
        m[a][a] = 1.0
        for b in sids:
            if a != b:
                m[a][b] = rho
    return m


def test_single_strategy_equals_weight_times_direction():
    out, nb = corr_adjusted_net_sharpe({'AAA': [('s1', 1)]}, _sim(['s1']), {'s1': 3.0})
    assert nb == 0
    assert out['AAA'] == pytest.approx(3.0)          # w1^2 / sqrt(w1^2) = w1


def test_single_short_is_negative():
    out, _ = corr_adjusted_net_sharpe({'AAA': [('s1', -1)]}, _sim(['s1']), {'s1': 4.0})
    assert out['AAA'] == pytest.approx(-4.0)


def test_n_independent_same_direction_sqrtN():
    sids = ['s1', 's2', 's3', 's4']
    contribs = {'AAA': [(s, 1) for s in sids]}
    out, nb = corr_adjusted_net_sharpe(contribs, _sim(sids, 0.0), {s: 2.0 for s in sids})
    assert nb == 0
    assert out['AAA'] == pytest.approx(4.0)          # sqrt(4) * 2 = 4


def test_rho_one_same_direction_no_double_count():
    sids = ['s1', 's2', 's3', 's4']
    contribs = {'AAA': [(s, 1) for s in sids]}
    out, _ = corr_adjusted_net_sharpe(contribs, _sim(sids, 1.0), {s: 2.0 for s in sids})
    assert out['AAA'] == pytest.approx(2.0)          # duplicate gets zero extra credit


def test_unequal_uncorrelated_is_quadrature():
    contribs = {'AAA': [('s1', 1), ('s2', 1)]}
    out, _ = corr_adjusted_net_sharpe(contribs, _sim(['s1', 's2'], 0.0), {'s1': 5.0, 's2': 1.0})
    assert out['AAA'] == pytest.approx(math.sqrt(26.0))   # 26 / sqrt(26)


def test_two_opposing_equal_cancels_to_zero():
    contribs = {'AAA': [('s1', 1), ('s2', -1)]}
    out, _ = corr_adjusted_net_sharpe(contribs, _sim(['s1', 's2'], 0.0), {'s1': 5.0, 's2': 5.0})
    assert out['AAA'] == pytest.approx(0.0)


def test_opposing_unequal_signed_dominance():
    contribs = {'AAA': [('s1', 1), ('s2', -1)]}
    out, _ = corr_adjusted_net_sharpe(contribs, _sim(['s1', 's2'], 0.0), {'s1': 5.0, 's2': 4.0})
    assert out['AAA'] == pytest.approx(9.0 / math.sqrt(41.0))   # ~1.405, net long


def test_missing_pair_uses_sparse_default():
    # sim has only diagonals -> off-diagonal falls back to 0.05.
    sim = {'s1': {'s1': 1.0}, 's2': {'s2': 1.0}}
    out, _ = corr_adjusted_net_sharpe({'AAA': [('s1', 1), ('s2', 1)]}, sim, {'s1': 2.0, 's2': 2.0})
    assert out['AAA'] == pytest.approx(8.0 / math.sqrt(8.4))    # q = 8 + 2*2*2*0.05 = 8.4


def test_non_psd_backstop_no_nan():
    # 3 co-firing, sims {0.8,0.8,0.05}, dirs (L,S,S): q = -0.10 < eps -> diagonal backstop.
    sim = {'s1': {'s1': 1.0, 's2': 0.8, 's3': 0.8},
           's2': {'s2': 1.0, 's1': 0.8, 's3': 0.05},
           's3': {'s3': 1.0, 's1': 0.8, 's2': 0.05}}
    contribs = {'AAA': [('s1', 1), ('s2', -1), ('s3', -1)]}
    out, nb = corr_adjusted_net_sharpe(contribs, sim, {'s1': 1.0, 's2': 1.0, 's3': 1.0})
    assert nb == 1
    assert math.isfinite(out['AAA'])
    assert out['AAA'] == pytest.approx(-1.0 / math.sqrt(3.0))   # num=-1, den=sqrt(diag=3)


def test_zero_direction_skipped():
    out, nb = corr_adjusted_net_sharpe({'AAA': [('s1', 1), ('s2', 0)]}, _sim(['s1', 's2']),
                                       {'s1': 3.0, 's2': 9.9})
    assert out['AAA'] == pytest.approx(3.0)          # s2 (d=0) ignored
