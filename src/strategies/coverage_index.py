"""SP-7 Phase C — importable (ticker × month) cumulative-bar coverage index.

Hoisted verbatim from scripts/build_tier_membership.py (Phase B) so the LIVE
resolve path shares ONE parquet read per process instead of ParquetCoverage's
full re-read per month-miss (src/strategies/_db_adapters.py:58).

Month-granularity note: counts are cumulative through the END of each month.
For live resolution (as_of = today) this equals day-granular counting — no
bars exist beyond today. For historical mid-month as_of it would count bars
from later in that month: do NOT use for PIT backtests (PrecomputedResolver
owns that path).
"""
from __future__ import annotations

from datetime import date

MIN_BARS = 60  # mirrors ParquetCoverage min_bars (src/strategies/_db_adapters.py:45)


class CoverageIndex:
    """(ticker × month) cumulative bar counts from ONE parquet read."""

    def __init__(self, prices_df, min_bars: int = MIN_BARS):
        df = prices_df.copy()
        df['month'] = df['date'].astype(str).str[:7]
        counts = (df.groupby(['ticker', 'month']).size()
                    .unstack(fill_value=0).sort_index(axis=1)
                    .cumsum(axis=1))
        self._counts = counts
        self._min = min_bars

    @classmethod
    def from_parquet(cls, path='data/master/prices.parquet', min_bars=MIN_BARS):
        import pandas as pd
        df = pd.read_parquet(path, columns=['ticker', 'date'])
        from src.pipeline.quarantine_filter import filter_quarantined
        df = filter_quarantined(df, 'prices.parquet')
        return cls(df, min_bars)

    def has_floor(self, symbol: str, as_of: date) -> bool:
        m = as_of.isoformat()[:7]
        if symbol not in self._counts.index:
            return False
        row = self._counts.loc[symbol]
        cols = [c for c in row.index if c <= m]
        if not cols:
            return False
        return int(row[cols[-1]]) >= self._min
