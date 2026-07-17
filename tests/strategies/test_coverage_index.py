"""SP-7 Phase C Task 1 — importable CoverageIndex (hoisted from build_tier_membership)."""
from datetime import date

import pandas as pd
import pytest


def _prices_df():
    # AAPL: 70 bars Jan-Mar 2026 (passes 60-floor by March); NEWT: 5 bars (fails)
    rows = []
    for i in range(70):
        rows.append({"ticker": "AAPL", "date": (pd.Timestamp("2026-01-02") + pd.Timedelta(days=i)).strftime("%Y-%m-%d")})
    for i in range(5):
        rows.append({"ticker": "NEWT", "date": (pd.Timestamp("2026-03-02") + pd.Timedelta(days=i)).strftime("%Y-%m-%d")})
    return pd.DataFrame(rows)


def test_has_floor_basic():
    from src.strategies.coverage_index import CoverageIndex
    idx = CoverageIndex(_prices_df(), min_bars=60)
    assert idx.has_floor("AAPL", date(2026, 3, 31)) is True
    assert idx.has_floor("NEWT", date(2026, 3, 31)) is False      # only 5 bars
    assert idx.has_floor("MISSING", date(2026, 3, 31)) is False   # absent symbol
    assert idx.has_floor("AAPL", date(2025, 12, 31)) is False     # before any bars


def test_min_bars_constant_matches_parquet_coverage():
    from src.strategies.coverage_index import MIN_BARS
    assert MIN_BARS == 60  # mirrors ParquetCoverage default (_db_adapters.py)


def test_equivalence_with_parquet_coverage(tmp_path, monkeypatch):
    """At month-end as_of (the live case: as_of=today, no future bars), the
    month-granular CoverageIndex equals the day-granular ParquetCoverage."""
    import src.pipeline.quarantine_filter as qf
    monkeypatch.setattr(qf, "filter_quarantined", lambda df, t: df)
    df = _prices_df()
    pq_path = tmp_path / "prices.parquet"
    df.to_parquet(pq_path, index=False)

    from src.strategies._db_adapters import ParquetCoverage
    from src.strategies.coverage_index import CoverageIndex
    legacy = ParquetCoverage(prices_path=str(pq_path), min_bars=60)
    fast = CoverageIndex.from_parquet(path=str(pq_path), min_bars=60)
    as_of = date(2026, 3, 31)  # >= max bar date → no within-month peek possible
    for sym in ("AAPL", "NEWT", "MISSING"):
        assert fast.has_floor(sym, as_of) == legacy.has_floor(sym, as_of), sym


def test_build_tier_membership_imports_hoisted_class():
    """build_tier_membership must consume the hoisted module (no local copy)."""
    from pathlib import Path
    src = Path("scripts/build_tier_membership.py").read_text()
    assert "from src.strategies.coverage_index import" in src
    assert "class CoverageIndex" not in src
