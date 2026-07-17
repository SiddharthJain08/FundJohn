"""Tests for SP-2 Phase A: pipeline helpers that consume union_universe.

Phase A scope: helpers exist and are tested; legacy call sites are
untouched. Task 13.1 wires the production CLI adapters.
"""
from datetime import date
from unittest.mock import MagicMock


def test_alpaca_options_iterates_union_filtered_to_options_eligible():
    fake_resolver = MagicMock()
    fake_resolver.union_universe.return_value = ["AAPL", "MSFT", "XYZQ"]
    fake_meta = {
        "AAPL": MagicMock(options_eligible=True),
        "MSFT": MagicMock(options_eligible=True),
        "XYZQ": MagicMock(options_eligible=False),
    }
    from src.pipeline.backfillers.alpaca_options import _select_archive_universe
    out = _select_archive_universe(
        as_of=date(2026, 5, 22), resolver=fake_resolver,
        meta_lookup=lambda s, d: fake_meta[s],
    )
    assert sorted(out) == ["AAPL", "MSFT"]


def test_sentiment_uses_live_plus_candidate():
    fake_resolver = MagicMock()
    fake_resolver.union_universe.return_value = ["AAPL", "TSLA"]
    from src.pipeline.run_sentiment_step import _select_sentiment_universe
    out = _select_sentiment_universe(as_of=date(2026, 5, 22), resolver=fake_resolver)
    fake_resolver.union_universe.assert_called_with(date(2026, 5, 22), states=("live", "candidate"))
    assert out == ["AAPL", "TSLA"]
