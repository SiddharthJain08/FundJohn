#!/usr/bin/env python3
"""SP-7 Phase B — one-time tier-membership precompute.

Builds data/universe_tier_membership_<run_id>.parquet with one row per
(tier, snapshot_date): the sorted member list after predicate + coverage
floor. Also writes a JSON sidecar with per-tier N series + data-level
nesting diagnostics (|in_sp500 ∧ ¬in_r1000| etc.).

Usage:
  python3 scripts/build_tier_membership.py --run-id ladder-20260608 \
      --start 2021-07-01 --end 2026-06-05
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

LADDER_TIERS = ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')
MIN_BARS = 60  # mirrors ParquetCoverage min_bars (src/strategies/_db_adapters.py:45)


def snapshot_dates(start: date, end: date) -> list[date]:
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        last = date(cur.year, cur.month,
                    calendar.monthrange(cur.year, cur.month)[1])
        out.append(min(last, end))
        cur = last + timedelta(days=1)
    return out


class CoverageIndex:
    """(ticker × month) cumulative bar counts from ONE parquet read."""

    def __init__(self, prices_df, min_bars: int = MIN_BARS):
        import pandas as pd
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


def tiers_for_rows(rows, as_of: date, coverage) -> dict[str, list[str]]:
    from src.strategies import universe_default as ud
    preds = {t: getattr(ud, t) for t in LADDER_TIERS}
    out = {t: [] for t in LADDER_TIERS}
    for row in rows:
        meta = row.metadata
        if not coverage.has_floor(meta.symbol, as_of):
            continue
        for t, p in preds.items():
            try:
                if p(meta, as_of):
                    out[t].append(meta.symbol)
            except Exception:
                continue
    return {t: sorted(v) for t, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--out-dir', default='data')
    args = ap.parse_args()

    import pandas as pd
    from src.strategies._db_adapters import PostgresMetadataDB

    db = PostgresMetadataDB(os.environ['POSTGRES_URI'])
    cov = CoverageIndex.from_parquet()
    dates = snapshot_dates(date.fromisoformat(args.start),
                           date.fromisoformat(args.end))
    records, n_series, diags = [], {t: {} for t in LADDER_TIERS}, []
    for snap in dates:
        rows = db.fetch_metadata_as_of(snap)
        members = tiers_for_rows(rows, snap, cov)
        for t in LADDER_TIERS:
            records.append({'run_id': args.run_id, 'tier': t,
                            'snapshot_date': snap.isoformat(),
                            'symbols': members[t]})
            n_series[t][snap.isoformat()] = len(members[t])
        # data-level diagnostic (predicates force nesting; this measures the RAW flags)
        raw = {m.metadata.symbol: m.metadata for m in rows}
        sp_not_r1 = sum(1 for m in raw.values() if m.in_sp500 and not m.in_r1000)
        diags.append({'snapshot_date': snap.isoformat(),
                      'sp500_not_in_r1000_raw': sp_not_r1,
                      'n_rows': len(rows)})
        print(f'[membership] {snap} ' +
              ' '.join(f'{t}={len(members[t])}' for t in LADDER_TIERS))

    out = Path(args.out_dir) / f'universe_tier_membership_{args.run_id}.parquet'
    pd.DataFrame(records).to_parquet(out, index=False)
    sidecar = out.with_suffix('.json')
    sidecar.write_text(json.dumps(
        {'run_id': args.run_id, 'window': [args.start, args.end],
         'n_series': n_series, 'diagnostics': diags}, indent=2))
    print(f'[membership] DONE artifact={out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
