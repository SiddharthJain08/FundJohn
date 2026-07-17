"""SP-7 Phase C Task 3 — live-path resolver uses CoverageIndex; perf smoke."""
import os
import time
from datetime import date
from pathlib import Path

import pytest


def test_cli_block_uses_coverage_index():
    src = Path("src/strategies/universe_resolver.py").read_text()
    main_block = src[src.index('if __name__ == "__main__"'):]
    assert "CoverageIndex.from_parquet" in main_block
    assert "ParquetCoverage()" not in main_block


def test_threshold_proposals_factory_uses_coverage_index():
    src = Path("src/execution/universe_threshold_proposals.py").read_text()
    assert "CoverageIndex.from_parquet" in src


def test_grid_cli_untouched_keeps_parquet_coverage():
    """Backtest PIT path must NOT adopt the month-granular index."""
    src = Path("src/backtest/universe_grid_cli.py").read_text()
    assert "CoverageIndex" not in src


@pytest.mark.integration
def test_union_resolve_under_10s_warm():
    """Spec §3.2 perf acceptance: warm 67-strategy union <10s on the loaded box."""
    if not os.environ.get("POSTGRES_URI"):
        pytest.skip("POSTGRES_URI not set")
    if not Path("/root/openclaw/data/master/prices.parquet").exists():
        pytest.skip("master parquet absent")
    from src.execution.live_universe import build_resolver  # Task 5 module
    resolver = build_resolver()
    resolver.union_universe(date.today())          # cold: builds index + memo
    t0 = time.monotonic()
    out = resolver.union_universe(date.today())    # warm
    assert time.monotonic() - t0 < 10.0
    assert len(out) >= 200
