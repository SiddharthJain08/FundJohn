"""Tests for strategy_overlap — pairwise signal-overlap computation."""
from __future__ import annotations
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from metrics import strategy_overlap as so  # noqa: E402


def test_aggregate_empty_signals():
    out = so.aggregate_overlap([])
    assert out == []


def test_aggregate_two_strategies_same_ticker_date():
    today = date.today()
    sigs = [
        {'strategy_id': 's1', 'signal_date': today, 'ticker': 'AAPL', 'regime_state': 'LOW_VOL'},
        {'strategy_id': 's2', 'signal_date': today, 'ticker': 'AAPL', 'regime_state': 'LOW_VOL'},
    ]
    rows = so.aggregate_overlap(sigs)
    # One regime-specific row + one regime='ANY' all-regimes row
    pair_rows = [r for r in rows if (r['strategy_a'], r['strategy_b']) == ('s1', 's2')]
    assert len(pair_rows) == 2
    assert any(r['regime_state'] == 'ANY' for r in pair_rows)
    assert any(r['regime_state'] == 'LOW_VOL' for r in pair_rows)
    for r in pair_rows:
        assert r['overlap_count'] == 1
        assert r['a_signal_count'] == 1
        assert r['b_signal_count'] == 1
        # Jaccard = 1 / (1 + 1 - 1) = 1.0
        assert r['jaccard_idx'] == 1.0


def test_canonical_ordering():
    """Always emit strategy_a < strategy_b lexicographically."""
    today = date.today()
    sigs = [
        {'strategy_id': 'zzz_strategy', 'signal_date': today, 'ticker': 'X', 'regime_state': 'LOW_VOL'},
        {'strategy_id': 'aaa_strategy', 'signal_date': today, 'ticker': 'X', 'regime_state': 'LOW_VOL'},
    ]
    rows = so.aggregate_overlap(sigs)
    for r in rows:
        assert r['strategy_a'] < r['strategy_b']


def test_no_overlap_when_different_tickers():
    today = date.today()
    sigs = [
        {'strategy_id': 's1', 'signal_date': today, 'ticker': 'AAPL', 'regime_state': 'LOW_VOL'},
        {'strategy_id': 's2', 'signal_date': today, 'ticker': 'MSFT', 'regime_state': 'LOW_VOL'},
    ]
    rows = so.aggregate_overlap(sigs)
    # No pair should have overlap_count > 0 because tickers differ
    pair_rows = [r for r in rows if (r['strategy_a'], r['strategy_b']) == ('s1', 's2')]
    # Either no pair rows emitted, or overlap_count is 0.
    if pair_rows:
        assert all(r['overlap_count'] == 0 for r in pair_rows)
