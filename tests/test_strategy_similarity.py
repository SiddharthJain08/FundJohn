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
