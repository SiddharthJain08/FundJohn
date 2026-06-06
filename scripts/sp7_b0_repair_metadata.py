#!/usr/bin/env python3
"""SP-7 Phase B B0 — metadata coherence repair.

Repairs the v1/v2 ghost-row incoherence (mega-caps missing from historical
r1000/r3000; in_sp500 undercount) and the degenerate live_daily snapshots
2026-05-25..2026-06-04 (in_r3000=0, market_cap=0).

UPDATE-based supersede: recompute derived columns (in_sp500, in_r1000,
in_r3000, market_cap) per (snapshot_date, symbol) and UPDATE rows whose
values differ, flipping source_tag to 'backfill_5y_v3'. NEVER deletes.

Usage:
  OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 python3 scripts/sp7_b0_repair_metadata.py \
      --months --start 2021-01-01 --end 2026-05-31 [--dry-run] [--resume]
  OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 python3 scripts/sp7_b0_repair_metadata.py \
      --dailies [--dry-run]

Exit codes: 0 ok · 1 error · 2 gate refused.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

SOURCE_TAG = 'backfill_5y_v3'
DEGENERATE_DAILIES = [
    '2026-05-25', '2026-05-26', '2026-05-27', '2026-05-28', '2026-05-29',
    '2026-06-01', '2026-06-02', '2026-06-03', '2026-06-04',
]
DERIVED = ('in_sp500', 'in_r1000', 'in_r3000', 'market_cap')


def check_overwrite_gate() -> None:
    if os.environ.get('OPENCLAW_BACKFILL_ALLOW_OVERWRITE') != '1':
        print('[b0] REFUSED: set OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 '
              '(documented supersede gate)', file=sys.stderr)
        sys.exit(2)


def month_ends(start: date, end: date) -> list[date]:
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        last = date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
        out.append(min(last, end))
        cur = (last + timedelta(days=1))
    return out


def _norm(v):
    """pandas NaN -> None; passthrough otherwise."""
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    return v


def diff_derived(existing, rebuilt) -> list[dict]:
    """Rows (dicts incl. symbol) from `rebuilt` whose DERIVED cols differ
    from `existing` (bool compare for flags; $1 epsilon for market_cap).
    UPDATE-only: symbols absent from `existing` are ignored."""
    ex = existing.set_index('symbol')
    updates = []
    for row in rebuilt.itertuples():
        if row.symbol not in ex.index:
            continue
        old = ex.loc[row.symbol]
        upd, changed = {'symbol': row.symbol}, False
        for col in DERIVED:
            new_v, old_v = _norm(getattr(row, col)), _norm(old[col])
            if col == 'market_cap':
                differs = ((old_v is None) != (new_v is None)
                           or (old_v is not None and new_v is not None
                               and abs(float(old_v) - float(new_v)) > 1.0))
            else:
                differs = bool(old_v) != bool(new_v)
            upd[col] = new_v
            changed = changed or differs
        if changed:
            updates.append(upd)
    return updates


def _audit(cur, chunk_key: str, status: str, rows: int = 0, err: str | None = None):
    cur.execute(
        """INSERT INTO backfill_audit
             (target, chunk_key, started_at, ended_at, status, rows_written,
              source_tag, sha256, error_text)
           VALUES ('metadata', %s, NOW(), NOW(), %s, %s, %s, NULL, %s)""",
        (chunk_key, status, rows, SOURCE_TAG, err))


def repair_month(pg, snap: date, *, dry_run: bool) -> int:
    import pandas as pd
    from src.pipeline.backfillers.universe_metadata import build_month_snapshot
    from src.pipeline.market_cap_lookup import build_market_cap_lookup

    with pg.cursor() as cur:
        cur.execute(
            """SELECT symbol, in_sp500, in_r1000, in_r3000, market_cap
                 FROM ticker_metadata_snapshots WHERE snapshot_date = %s""",
            (snap,))
        cols = [d.name for d in cur.description]
        existing = pd.DataFrame(cur.fetchall(), columns=cols)
    if existing.empty:
        print(f'[b0] {snap} no rows — skip')
        return 0
    universe = sorted(existing.symbol)
    caps = build_market_cap_lookup(universe, snap)
    rebuilt = build_month_snapshot(snap, universe, pg, market_cap_lookup=caps)
    updates = diff_derived(existing, rebuilt)
    print(f'[b0] {snap} rows={len(existing)} changed={len(updates)}')
    if dry_run or not updates:
        return len(updates)
    with pg.cursor() as cur:
        from psycopg2.extras import execute_batch
        execute_batch(cur, f"""
            UPDATE ticker_metadata_snapshots
               SET in_sp500=%(in_sp500)s, in_r1000=%(in_r1000)s,
                   in_r3000=%(in_r3000)s, market_cap=%(market_cap)s,
                   source_tag='{SOURCE_TAG}'
             WHERE snapshot_date=%(snap)s AND symbol=%(symbol)s""",
            [{**u, 'snap': snap} for u in updates], page_size=500)
        _audit(cur, f'{snap.isoformat()}:metadata:repair_v3', 'promoted', len(updates))
    pg.commit()
    return len(updates)


def repair_dailies(pg, *, dry_run: bool) -> int:
    """Fill ONLY the failed derived columns on the 9 degenerate daily
    snapshots: market_cap (shares×close) then rank-based in_r1000/in_r3000.
    in_sp500 + observed columns untouched (they were written correctly)."""
    import pandas as pd
    from src.pipeline.market_cap_lookup import build_market_cap_lookup
    from src.pipeline.backfillers.universe_metadata import rank_in_r1000_r3000

    total = 0
    for iso in DEGENERATE_DAILIES:
        snap = date.fromisoformat(iso)
        with pg.cursor() as cur:
            cur.execute(
                """SELECT symbol, status, tradable, market_cap
                     FROM ticker_metadata_snapshots WHERE snapshot_date=%s""",
                (snap,))
            df = pd.DataFrame(cur.fetchall(),
                              columns=[d.name for d in cur.description])
        if df.empty:
            print(f'[b0-dailies] {iso} no rows — skip')
            continue
        caps = build_market_cap_lookup(sorted(df.symbol), snap)
        df['market_cap'] = df.symbol.map(lambda s: caps.get(s))
        r1000, r3000 = rank_in_r1000_r3000(df)
        df['in_r1000'] = df.symbol.isin(r1000)
        df['in_r3000'] = df.symbol.isin(r3000)
        n = int(df.in_r3000.sum())
        print(f'[b0-dailies] {iso} rows={len(df)} r1000={int(df.in_r1000.sum())} r3000={n}')
        if dry_run:
            continue
        with pg.cursor() as cur:
            from psycopg2.extras import execute_batch
            execute_batch(cur, """
                UPDATE ticker_metadata_snapshots
                   SET market_cap=%(market_cap)s, in_r1000=%(in_r1000)s,
                       in_r3000=%(in_r3000)s
                 WHERE snapshot_date=%(snap)s AND symbol=%(symbol)s""",
                [{'symbol': r.symbol,
                  'market_cap': None if r.market_cap != r.market_cap else r.market_cap,
                  'in_r1000': bool(r.in_r1000), 'in_r3000': bool(r.in_r3000),
                  'snap': snap} for r in df.itertuples()], page_size=500)
            _audit(cur, f'{iso}:metadata:repair_dailies_v3', 'promoted', len(df))
        pg.commit()
        total += len(df)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--months', action='store_true')
    ap.add_argument('--dailies', action='store_true')
    ap.add_argument('--start', default='2021-01-01')
    ap.add_argument('--end', default='2026-05-31')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--resume', action='store_true',
                    help='skip months already audited promoted for repair_v3')
    args = ap.parse_args()
    if not (args.months or args.dailies):
        ap.error('need --months and/or --dailies')
    check_overwrite_gate()

    import psycopg2
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        if args.months:
            done = set()
            if args.resume:
                with pg.cursor() as cur:
                    cur.execute("""SELECT chunk_key FROM backfill_audit
                                   WHERE source_tag=%s AND status='promoted'
                                     AND chunk_key LIKE '%%:metadata:repair_v3'""",
                                (SOURCE_TAG,))
                    done = {r[0].split(':')[0] for r in cur.fetchall()}
            for snap in month_ends(date.fromisoformat(args.start),
                                   date.fromisoformat(args.end)):
                if snap.isoformat() in done:
                    print(f'[b0] {snap} already promoted — resume-skip')
                    continue
                repair_month(pg, snap, dry_run=args.dry_run)
        if args.dailies:
            repair_dailies(pg, dry_run=args.dry_run)
        print('[b0] DONE')
        return 0
    finally:
        pg.close()


if __name__ == '__main__':
    sys.exit(main())
