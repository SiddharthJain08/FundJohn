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
# Universe security type, Phase 1: composed tiers (existing tier AND common_stock).
# Kept OUT of LADDER_TIERS on purpose — the ladder/shrink consumers iterate
# LADDER_TIERS and must see exactly the tiers they always did; stocks_* are
# extra rows in the same artifact, read only by PrecomputedResolver(tier=...).
STOCK_TIERS = ('stocks_sp500', 'stocks_r1000', 'stocks_r3000', 'stocks_liquid')
ALL_TIERS = LADDER_TIERS + STOCK_TIERS
PROFILE_CACHE = ROOT / 'data' / '.cache' / 'fmp_profile.json'

from src.strategies.coverage_index import CoverageIndex, MIN_BARS  # noqa: E402


def snapshot_dates(start: date, end: date) -> list[date]:
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        last = date(cur.year, cur.month,
                    calendar.monthrange(cur.year, cur.month)[1])
        out.append(min(last, end))
        cur = last + timedelta(days=1)
    return out


def build_security_type_overlay(db_types: dict, profiles: dict) -> dict:
    """The latest-known security type per symbol, applied to EVERY snapshot
    date (static attribute; spec 2026-10-04 §1 "History").

    db_types:  {symbol: latest non-NULL ticker_metadata_snapshots.security_type}
    profiles:  the vendor profile cache (data/.cache/fmp_profile.json).

    FALLBACK (documented): until the first daily snapshot written after
    migration 163 has run, the DB column is NULL everywhere, so a symbol with
    no DB value takes security_type_from_profile(profile). The DB value wins
    when both exist. Symbols in neither stay unknown (None) — common_stock then
    admits them only if in_sp500."""
    from src.strategies.universe_meta import security_types_from_profiles
    overlay = security_types_from_profiles(profiles)
    overlay.update({k: v for k, v in (db_types or {}).items() if v})
    return overlay


def load_profile_cache(path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def tiers_for_rows(rows, as_of: date, coverage) -> dict[str, list[str]]:
    from src.strategies import universe_default as ud
    preds = {t: getattr(ud, t) for t in ALL_TIERS}
    out = {t: [] for t in ALL_TIERS}
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
    ap.add_argument('--profile-cache', default=str(PROFILE_CACHE),
                    help='vendor profile cache; fallback security-type source '
                         'for symbols whose DB security_type is still NULL')
    args = ap.parse_args()

    import pandas as pd
    from src.strategies._db_adapters import PostgresMetadataDB

    db = PostgresMetadataDB(os.environ['POSTGRES_URI'])
    cov = CoverageIndex.from_parquet()
    dates = snapshot_dates(date.fromisoformat(args.start),
                           date.fromisoformat(args.end))
    from src.strategies.universe_meta import overlay_security_types
    overlay = build_security_type_overlay(db.fetch_latest_security_types(),
                                          load_profile_cache(args.profile_cache))
    print(f'[membership] security-type overlay: {len(overlay)} symbols '
          f'(db + profile-cache fallback)')
    records, n_series, diags = [], {t: {} for t in ALL_TIERS}, []
    for snap in dates:
        rows = overlay_security_types(db.fetch_metadata_as_of(snap), overlay)
        members = tiers_for_rows(rows, snap, cov)
        for t in ALL_TIERS:
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
              ' '.join(f'{t}={len(members[t])}' for t in ALL_TIERS))

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
