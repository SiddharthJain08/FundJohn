"""Regression tests for _atomic_to_parquet and _promote_chunk atomicity.

Verifies that:
  1. _atomic_to_parquet writes via a .tmp-promote sibling and then replaces the
     target — never writing directly to the master path.
  2. The final file contains the correct content.
  3. The tmp file is absent after a successful write.
  4. _promote_chunk calls _atomic_to_parquet (not to_parquet directly on the
     master) — confirmed by monkeypatching _atomic_to_parquet and asserting it
     is invoked while the master path itself is never opened for writing.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _sample_df(tickers=("AAPL",), year=2023):
    import pandas as pd
    rows = [
        {"ticker": t, "date": f"{year}-01-03", "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.5, "volume": 1_000_000, "source": None}
        for t in tickers
    ]
    return pd.DataFrame(rows)


# ── _atomic_to_parquet ─────────────────────────────────────────────────────────

class TestAtomicToParquet:
    def test_writes_correct_content(self, tmp_path):
        from scripts.backfill_universe_5y import _atomic_to_parquet
        dest = tmp_path / "prices.parquet"
        df = _sample_df()
        _atomic_to_parquet(df, dest)

        result = pd.read_parquet(dest)
        assert list(result["ticker"]) == ["AAPL"]
        assert list(result["date"]) == ["2023-01-03"]

    def test_tmp_file_removed_after_success(self, tmp_path):
        from scripts.backfill_universe_5y import _atomic_to_parquet
        dest = tmp_path / "prices.parquet"
        _atomic_to_parquet(_sample_df(), dest)

        tmp = dest.with_suffix(".tmp-promote")
        assert not tmp.exists(), "tmp-promote must be cleaned up after atomic replace"

    def test_uses_sibling_tmp_name(self, tmp_path):
        """Verify the intermediate file is written next to the target, not elsewhere."""
        from scripts.backfill_universe_5y import _atomic_to_parquet

        written_paths: list[Path] = []
        original_to_parquet = pd.DataFrame.to_parquet

        def tracking_to_parquet(self, path, **kwargs):
            written_paths.append(Path(path))
            return original_to_parquet(self, path, **kwargs)

        dest = tmp_path / "prices.parquet"
        with patch.object(pd.DataFrame, "to_parquet", tracking_to_parquet):
            _atomic_to_parquet(_sample_df(), dest)

        # Exactly one write call, and it must be to the .tmp-promote sibling
        assert len(written_paths) == 1
        assert written_paths[0] == dest.with_suffix(".tmp-promote"), (
            f"Expected write to {dest.with_suffix('.tmp-promote')}, got {written_paths[0]}"
        )
        # Master path was never opened for writing by to_parquet
        assert dest not in written_paths

    def test_overwrites_existing_file(self, tmp_path):
        """A second write replaces the old content atomically."""
        from scripts.backfill_universe_5y import _atomic_to_parquet

        dest = tmp_path / "prices.parquet"
        df1 = _sample_df(tickers=("AAPL",))
        df2 = _sample_df(tickers=("MSFT",))

        _atomic_to_parquet(df1, dest)
        _atomic_to_parquet(df2, dest)

        result = pd.read_parquet(dest)
        assert list(result["ticker"]) == ["MSFT"]

    def test_stray_tmp_overwritten_not_error(self, tmp_path):
        """A leftover .tmp-promote from a prior killed run is silently overwritten."""
        from scripts.backfill_universe_5y import _atomic_to_parquet

        dest = tmp_path / "prices.parquet"
        tmp = dest.with_suffix(".tmp-promote")
        tmp.write_bytes(b"garbage from prior run")

        _atomic_to_parquet(_sample_df(), dest)  # must not raise

        assert dest.exists()
        assert not tmp.exists()


# ── _promote_chunk uses _atomic_to_parquet ────────────────────────────────────

class TestPromoteChunkAtomic:
    def test_promote_chunk_calls_atomic_helper(self, tmp_path):
        """_promote_chunk must delegate writes to _atomic_to_parquet, not call
        DataFrame.to_parquet directly on the master path."""
        import scripts.backfill_universe_5y as mod

        atomic_calls: list[tuple] = []
        direct_parquet_calls: list[Path] = []

        original_to_parquet = pd.DataFrame.to_parquet

        def spy_to_parquet(self, path, **kwargs):
            direct_parquet_calls.append(Path(path))
            return original_to_parquet(self, path, **kwargs)

        def spy_atomic(df, path):
            atomic_calls.append((df, path))
            # Still actually write so the function completes normally
            original_atomic(df, path)

        original_atomic = mod._atomic_to_parquet

        master = tmp_path / "prices.parquet"
        df = _sample_df()

        with patch.object(pd.DataFrame, "to_parquet", spy_to_parquet), \
             patch.object(mod, "_atomic_to_parquet", spy_atomic):
            rows = mod._promote_chunk(df, "AAPL", 2023, "backfill_5y_v1", master)

        assert rows == 1, "should report 1 new row promoted"
        assert len(atomic_calls) == 1, "_atomic_to_parquet should be called exactly once"
        assert atomic_calls[0][1] == master, "atomic call should target the master path"

        # The DataFrame.to_parquet spy must NOT have been called with the master
        # path (only the .tmp-promote sibling, which happens inside _atomic_to_parquet
        # — but that call goes through the original, not through our spy here).
        for p in direct_parquet_calls:
            assert p != master, (
                f"to_parquet was called directly on master path {master} — "
                "must go through _atomic_to_parquet"
            )

    def test_promote_chunk_new_master(self, tmp_path):
        """When master doesn't exist yet, _promote_chunk creates it atomically."""
        import scripts.backfill_universe_5y as mod

        master = tmp_path / "subdir" / "prices.parquet"
        df = _sample_df()

        atomic_calls: list[Path] = []
        original_atomic = mod._atomic_to_parquet

        def spy_atomic(df, path):
            atomic_calls.append(path)
            original_atomic(df, path)

        with patch.object(mod, "_atomic_to_parquet", spy_atomic):
            rows = mod._promote_chunk(df, "AAPL", 2023, "backfill_5y_v1", master)

        assert rows == 1
        assert len(atomic_calls) == 1
        assert master.exists()
