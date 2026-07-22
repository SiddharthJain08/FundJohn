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
        import pandas as pd
        df = prices_df.copy()
        # numpy month truncation, not .astype(str).str[:7] — string ops on the
        # arrow-backed master parquet iterate element-wise (seconds vs ms).
        df['month'] = (pd.to_datetime(df['date'])
                       .to_numpy(dtype='datetime64[M]').astype(str))
        counts = (df.groupby(['ticker', 'month']).size()
                    .unstack(fill_value=0).sort_index(axis=1)
                    .cumsum(axis=1))
        self._counts = counts
        self._min = min_bars
        self._floor_sets: dict[str, frozenset] = {}

    @classmethod
    def from_parquet(cls, path='data/master/prices.parquet', min_bars=MIN_BARS,
                     cache_dir=None):
        """Load the prebuilt (ticker × month) counts when fresh, else rebuild
        from the master parquet and persist. Building from 15M+ price rows
        costs ~25s per process (2026-07-21: the resolver blew its 15s SLA on
        every invocation); the counts frame itself is ~13MB and loads in ms.
        Freshness key = parquet realpath + identity + quarantine-set digest,
        so an appended parquet, a new quarantine row, or a DIFFERENT source
        parquet all invalidate. cache_dir defaults to <parquet dir>/../cache
        (master → data/cache; a pytest tmp fixture caches inside its own tmp
        tree instead of polluting the prod cache). Cache I/O is best-effort:
        any failure falls back to a full rebuild."""
        import hashlib
        import os
        import pandas as pd
        from src.pipeline.quarantine_filter import filter_quarantined, _cached
        st = os.stat(path)
        if cache_dir is None:
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(path)),
                                     os.pardir, 'cache')
        try:
            bad = _cached('prices.parquet')
            qfp = (hashlib.sha1(repr(sorted(bad)).encode()).hexdigest()[:16]
                   if bad else '0')
        except Exception:
            qfp = 'na'
        # min_bars deliberately NOT in the key: _counts are raw cumulative
        # bars; the floor only thresholds at lookup time.
        key = f'{os.path.realpath(path)}:{st.st_mtime_ns}:{st.st_size}:{qfp}'
        cpath = os.path.join(cache_dir, 'coverage_index_counts.parquet')
        mpath = cpath + '.meta'
        try:
            with open(mpath) as fh:
                fresh = fh.read().strip() == key
            if fresh:
                counts = pd.read_parquet(cpath)
                obj = cls.__new__(cls)
                obj._counts = counts
                obj._min = min_bars
                obj._floor_sets = {}
                return obj
        except Exception:
            pass
        df = pd.read_parquet(path, columns=['ticker', 'date'])
        df = filter_quarantined(df, 'prices.parquet')
        obj = cls(df, min_bars)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            obj._counts.to_parquet(cpath + '.tmp')
            os.replace(cpath + '.tmp', cpath)
            with open(mpath + '.tmp', 'w') as fh:
                fh.write(key)
            os.replace(mpath + '.tmp', mpath)  # meta LAST: it is the validity gate
        except Exception:
            pass
        return obj

    def _floor_set(self, month: str) -> frozenset:
        """Symbols meeting the bar floor as of END of `month`, one vectorized
        pass, memoized. The resolver fans out per (strategy × ticker) with a
        single as_of — the old per-call `.loc[symbol]` row extraction was
        ~75 redundant pandas lookups per unique ticker (56s resolver runs,
        2026-07-21); membership in a precomputed set is O(1)."""
        s = self._floor_sets.get(month)
        if s is None:
            cols = [c for c in self._counts.columns if c <= month]
            if not cols:
                s = frozenset()
            else:
                ok = self._counts[cols[-1]].to_numpy() >= self._min
                s = frozenset(self._counts.index[ok])
            self._floor_sets[month] = s
        return s

    def has_floor(self, symbol: str, as_of: date) -> bool:
        return symbol in self._floor_set(as_of.isoformat()[:7])
