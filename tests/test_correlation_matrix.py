"""Tests for correlation_matrix — Pearson on PnL + Jaccard projection + blend."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import correlation_matrix as cm  # noqa: E402


def test_pearson_perfect_positive():
    assert abs(cm._pearson([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) - 1.0) < 0.001


def test_pearson_perfect_negative():
    assert abs(cm._pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) - (-1.0)) < 0.001


def test_pearson_clipped_to_window():
    # Clipping happens at the matrix level; pearson itself returns raw value.
    val = cm._pearson([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0])
    assert abs(val - 1.0) < 0.0001


def test_pearson_insufficient_returns_none():
    assert cm._pearson([1.0], [2.0]) is None  # need at least 2 paired observations


def test_correlation_matrix_diagonal_is_one():
    # Build correlation from a fixed pnl-by-(date, ticker) sample
    pnls_by_ticker_date = {
        'A': {'2025-01-01': 0.01, '2025-01-02': -0.02, '2025-01-03': 0.005},
        'B': {'2025-01-01': 0.02, '2025-01-02': -0.01, '2025-01-03': 0.01},
    }
    matrix = cm.correlation_from_pnl(pnls_by_ticker_date, ['A', 'B'])
    assert matrix['A']['A'] == 1.0
    assert matrix['B']['B'] == 1.0


def test_correlation_matrix_off_diagonal_clipped():
    # Two perfectly correlated tickers → clip from 1.0 to MAX_OFF_DIAGONAL (0.95)
    pnls_by_ticker_date = {
        'A': {'2025-01-01': 0.01, '2025-01-02': 0.02, '2025-01-03': 0.03},
        'B': {'2025-01-01': 0.01, '2025-01-02': 0.02, '2025-01-03': 0.03},
    }
    matrix = cm.correlation_from_pnl(pnls_by_ticker_date, ['A', 'B'])
    assert matrix['A']['B'] == cm.MAX_OFF_DIAGONAL
    assert matrix['B']['A'] == cm.MAX_OFF_DIAGONAL


def test_correlation_matrix_default_when_no_overlap_dates():
    """Two tickers traded on different dates → no paired observations →
    use SPARSE_DEFAULT."""
    pnls_by_ticker_date = {
        'A': {'2025-01-01': 0.01, '2025-01-02': 0.02},
        'B': {'2025-01-10': 0.01, '2025-01-11': 0.02},
    }
    matrix = cm.correlation_from_pnl(pnls_by_ticker_date, ['A', 'B'])
    assert matrix['A']['B'] == cm.SPARSE_DEFAULT
    assert matrix['B']['A'] == cm.SPARSE_DEFAULT


def test_blend_mixes_two_matrices():
    sigma_pnl = {'A': {'A': 1.0, 'B': 0.4}, 'B': {'A': 0.4, 'B': 1.0}}
    sigma_overlap = {'A': {'A': 1.0, 'B': 0.8}, 'B': {'A': 0.8, 'B': 1.0}}
    blended = cm.blend(sigma_pnl, sigma_overlap, alpha=0.5)
    # 0.5 * 0.4 + 0.5 * 0.8 = 0.6
    assert abs(blended['A']['B'] - 0.6) < 0.001
    # Diagonals stay at 1.0
    assert blended['A']['A'] == 1.0
