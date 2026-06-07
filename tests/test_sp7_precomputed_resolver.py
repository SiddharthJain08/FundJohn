"""SP-7 Phase B Task 6 — PrecomputedResolver: PIT bisect, future-guard, empty pre-window."""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.precomputed_resolver import PrecomputedResolver
from src.strategies.universe_resolver import AsOfInFutureError


@pytest.fixture
def artifact(tmp_path):
    df = pd.DataFrame([
        {'run_id': 'r1', 'tier': 'sp500', 'snapshot_date': '2024-01-31', 'symbols': ['AAPL', 'MSFT']},
        {'run_id': 'r1', 'tier': 'sp500', 'snapshot_date': '2024-02-29', 'symbols': ['AAPL', 'MSFT', 'NVDA']},
        {'run_id': 'r1', 'tier': 'tier_liquid', 'snapshot_date': '2024-01-31', 'symbols': ['AAPL', 'MSFT', 'ZZZ']},
    ])
    p = tmp_path / 'art.parquet'
    df.to_parquet(p, index=False)
    return p


def test_bisect_most_recent_snapshot_leq(artifact):
    r = PrecomputedResolver(artifact, 'sp500', today_fn=lambda: date(2026, 1, 1))
    assert r.resolve('any_strategy', date(2024, 2, 15)) == ['AAPL', 'MSFT']
    assert r.resolve('any_strategy', date(2024, 3, 15)) == ['AAPL', 'MSFT', 'NVDA']


def test_pre_window_is_empty(artifact):
    r = PrecomputedResolver(artifact, 'sp500', today_fn=lambda: date(2026, 1, 1))
    assert r.resolve('s', date(2023, 6, 1)) == []


def test_future_guard(artifact):
    r = PrecomputedResolver(artifact, 'sp500', today_fn=lambda: date(2024, 6, 1))
    with pytest.raises(AsOfInFutureError):
        r.resolve('s', date(2024, 7, 1))


def test_tier_isolation_and_unknown_tier(artifact):
    r = PrecomputedResolver(artifact, 'tier_liquid', today_fn=lambda: date(2026, 1, 1))
    assert 'ZZZ' in r.resolve('s', date(2024, 2, 15))
    with pytest.raises(ValueError):
        PrecomputedResolver(artifact, 'no_such_tier')
