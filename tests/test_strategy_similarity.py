"""Tests for strategy_similarity — co-firing Jaccard, return-corr blend, clustering."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import strategy_similarity as ss  # noqa: E402


def test_jaccard_identical_sets():
    assert ss.jaccard({1, 2, 3}, {1, 2, 3}) == 1.0


def test_jaccard_disjoint_sets():
    assert ss.jaccard({1, 2}, {3, 4}) == 0.0


def test_jaccard_half_overlap():
    assert ss.jaccard({1, 2}, {2, 3}) == 1.0 / 3.0  # |intersect|=1, |union|=3


def test_jaccard_empty_is_zero():
    assert ss.jaccard(set(), {1}) == 0.0


def test_overlap_similarity_matrix_diagonal_and_symmetry():
    sets = {
        'S1': {('2026W18', 'AAPL', 1), ('2026W18', 'MSFT', 1)},
        'S2': {('2026W18', 'AAPL', 1)},
    }
    m = ss.overlap_similarity(sets)
    assert m['S1']['S1'] == 1.0 and m['S2']['S2'] == 1.0
    assert m['S1']['S2'] == m['S2']['S1']
    assert abs(m['S1']['S2'] - 0.5) < 1e-9  # |intersect|=1, |union|=2


def test_adaptive_alpha_zero_at_no_obs():
    assert ss.adaptive_alpha(0) == 0.0


def test_adaptive_alpha_reaches_ceiling():
    assert abs(ss.adaptive_alpha(ss.ALPHA_FULL_OBS) - ss.RETURN_CORR_ALPHA_CEIL) < 1e-9
    assert abs(ss.adaptive_alpha(10 * ss.ALPHA_FULL_OBS) - ss.RETURN_CORR_ALPHA_CEIL) < 1e-9  # capped


def test_blend_pure_overlap_when_no_return_history():
    overlap = {'A': {'A': 1.0, 'B': 0.5}, 'B': {'A': 0.5, 'B': 1.0}}
    retcorr = {'A': {'A': 1.0, 'B': 0.9}, 'B': {'A': 0.9, 'B': 1.0}}
    n_obs = {('A', 'B'): 0}  # no joint return history -> alpha 0 -> pure overlap
    blended = ss.blend_similarity(overlap, retcorr, n_obs)
    assert abs(blended['A']['B'] - 0.5) < 1e-9


def test_blend_weights_return_corr_when_history_ample():
    overlap = {'A': {'A': 1.0, 'B': 0.2}, 'B': {'A': 0.2, 'B': 1.0}}
    retcorr = {'A': {'A': 1.0, 'B': 0.9}, 'B': {'A': 0.9, 'B': 1.0}}
    n_obs = {('A', 'B'): ss.ALPHA_FULL_OBS}  # alpha = ceiling 0.6
    blended = ss.blend_similarity(overlap, retcorr, n_obs)
    # 0.4*0.2 + 0.6*0.9 = 0.62
    assert abs(blended['A']['B'] - 0.62) < 1e-9
