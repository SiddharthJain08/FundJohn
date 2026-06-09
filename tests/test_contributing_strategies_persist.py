"""tests/test_contributing_strategies_persist.py

Part ③ (2026-06-08): persist the sizer's per-ticker corr-gate contributing
strategies so the dashboard ticker-alpha shows ONLY contributing strategies.
This tests the pure collapse of sized orders → {ticker: sorted unique strategies},
which is what gets upserted into cycle_contributing_strategies.

Run:
    pytest tests/test_contributing_strategies_persist.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.regime_blended_sizer_live import _collapse_contributing  # noqa: E402


class TestCollapseContributing:
    def test_per_ticker_unique_sorted(self):
        orders = [
            {'ticker': 'AMZN', 'contributing_strategies': ['S_b', 'S_a', 'S_b']},
            {'ticker': 'TSLA', 'contributing_strategies': ['S_c']},
        ]
        assert _collapse_contributing(orders) == {
            'AMZN': ['S_a', 'S_b'], 'TSLA': ['S_c']}

    def test_merges_multiple_orders_same_ticker(self):
        orders = [
            {'ticker': 'AMZN', 'contributing_strategies': ['S_a']},
            {'ticker': 'AMZN', 'contributing_strategies': ['S_b']},
        ]
        assert _collapse_contributing(orders) == {'AMZN': ['S_a', 'S_b']}

    def test_falls_back_to_strategy_id(self):
        orders = [{'ticker': 'NVDA', 'strategy_id': 'S_solo'}]
        assert _collapse_contributing(orders) == {'NVDA': ['S_solo']}

    def test_skips_synthetic_close_pseudo_strategies(self):
        orders = [
            {'ticker': 'SPY', 'contributing_strategies': ['__close_option_expiry__']},
            {'ticker': 'QQQ', 'contributing_strategies': ['S_real', '__close_option_orphan__']},
        ]
        out = _collapse_contributing(orders)
        assert 'SPY' not in out                 # nothing real → dropped
        assert out == {'QQQ': ['S_real']}

    def test_skips_orders_without_ticker_or_strategies(self):
        orders = [
            {'contributing_strategies': ['S_a']},          # no ticker
            {'ticker': 'X', 'contributing_strategies': []}, # no strategies
            {'ticker': 'Y'},                                # nothing
        ]
        assert _collapse_contributing(orders) == {}

    def test_empty(self):
        assert _collapse_contributing([]) == {}


class TestCollapseContributions:
    def test_collapse_contributions_keeps_signed_terms_per_ticker(self):
        from execution import regime_blended_sizer_live as live
        orders = [
            {'ticker': 'AAPL', 'contributions': [
                {'strategy_id': 'A', 'contribution': 3.0, 'direction': 1},
                {'strategy_id': 'B', 'contribution': -1.0, 'direction': -1}]},
            {'ticker': 'MSFT', 'contributions': [
                {'strategy_id': 'C', 'contribution': 2.0, 'direction': 1}]},
            {'ticker': 'NVDA', 'contributions': []},          # orphan close → skipped
        ]
        out = live._collapse_contributions(orders)
        assert out['AAPL'] == [
            {'strategy_id': 'A', 'contribution': 3.0, 'direction': 1},
            {'strategy_id': 'B', 'contribution': -1.0, 'direction': -1}]
        assert out['MSFT'] == [{'strategy_id': 'C', 'contribution': 2.0, 'direction': 1}]
        assert 'NVDA' not in out
