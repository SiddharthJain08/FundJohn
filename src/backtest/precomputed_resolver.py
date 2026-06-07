"""SP-7 Phase B — PrecomputedResolver.

Duck-types UniverseResolver.resolve(strategy_id, as_of) for the grid path
(_per_bar_simulate only calls .resolve), backed by the frozen membership
artifact: a dict lookup per bar — zero DB connections, zero parquet scans.
Replicates MockResolver's PIT semantics (most recent snapshot <= as_of) and
the AsOfInFutureError look-ahead guard.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date as _date
from pathlib import Path

from src.strategies.universe_resolver import AsOfInFutureError


class PrecomputedResolver:
    def __init__(self, artifact_path, tier: str,
                 today_fn=_date.today):
        import pandas as pd
        df = pd.read_parquet(Path(artifact_path))
        df = df[df['tier'] == tier]
        if df.empty:
            raise ValueError(f'tier {tier!r} not present in {artifact_path}')
        self._tier = tier
        pairs = sorted(
            (_date.fromisoformat(str(r.snapshot_date)[:10]), list(r.symbols))
            for r in df.itertuples())
        self._dates = [d for d, _ in pairs]
        self._members = {d: syms for d, syms in pairs}
        self._today_fn = today_fn

    def resolve(self, strategy_id: str, as_of: _date) -> list[str]:
        if as_of > self._today_fn():
            raise AsOfInFutureError(f'as_of {as_of} > today {self._today_fn()}')
        i = bisect_right(self._dates, as_of) - 1
        if i < 0:
            return []
        # Defensive copy: callers must not be able to mutate the stored
        # membership and poison subsequent bars (review hardening, Task 6).
        return list(self._members[self._dates[i]])
