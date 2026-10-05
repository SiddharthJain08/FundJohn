#!/usr/bin/env python3
"""Operator repair: re-derive in_r1000 / in_r3000 on stored snapshots.

Since ~2026-08-23 the weekly vendor-profile job supplied market caps for
ETFs/funds, which then took Russell top-N slots and displaced real stocks
(rank_in_r1000_r3000 did not look at security_type). The ranking is fixed
(NON_COMMON_SECURITY_TYPES excluded from the pool); this script re-derives the
two flags on ALREADY-STORED snapshot rows from their stored tradable / status /
market_cap plus the latest-known security type per symbol.

  python3 scripts/rederive_rank_flags.py --from 2026-08-20 [--to DATE] [--apply]

Dry-run is the DEFAULT (session read-only, nothing written). --apply issues,
per snapshot_date and in ONE transaction, only

  UPDATE ticker_metadata_snapshots SET in_r1000 = %s, in_r3000 = %s
   WHERE snapshot_date = %s AND symbol = %s

for rows whose value changes (no DELETE / INSERT / other column). A --from
earlier than 2026-08-01 needs --force-range (older snapshots are clean).
POSTGRES_URI comes from the environment.

AFTER an apply the tier membership artifact must be REBUILT
(scripts/build_tier_membership.py) for backtests to see the corrected month-ends.
Exit status is non-zero on any error; the failing date is rolled back.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pipeline.backfillers.universe_metadata import rank_in_r1000_r3000  # noqa: E402
from src.strategies._db_adapters import LATEST_SECURITY_TYPE_SQL  # noqa: E402

MIN_SAFE_FROM = date(2026, 8, 1)

DATES_SQL = (
    "SELECT DISTINCT snapshot_date FROM ticker_metadata_snapshots "
    "WHERE snapshot_date >= %s AND snapshot_date <= %s ORDER BY snapshot_date"
)
ROWS_SQL = (
    "SELECT symbol, tradable, status, market_cap, in_r1000, in_r3000 "
    "FROM ticker_metadata_snapshots WHERE snapshot_date = %s"
)
UPDATE_SQL = (
    "UPDATE ticker_metadata_snapshots SET in_r1000 = %s, in_r3000 = %s "
    "WHERE snapshot_date = %s AND symbol = %s"
)


def recompute_flags(rows: list[dict], types: dict) -> tuple[set, set]:
    """New (r1000, r3000) symbol sets from stored rows + latest-known types."""
    pool = [
        {
            'symbol': r['symbol'],
            'tradable': bool(r['tradable']),
            'status': r['status'],
            'market_cap': None if r['market_cap'] is None else float(r['market_cap']),
            'security_type': types.get(r['symbol']),
        }
        for r in rows
    ]
    return rank_in_r1000_r3000(pool)


def diff_flags(rows: list[dict], types: dict) -> dict:
    """{'changes': [(symbol, new_r1000, new_r3000)], counts...} for one date."""
    r1000, r3000 = recompute_flags(rows, types)
    changes = []
    n1 = n3 = 0
    leave = {'r1000': Counter(), 'r3000': Counter()}
    enter = {'r1000': Counter(), 'r3000': Counter()}
    for r in rows:
        sym = r['symbol']
        new1, new3 = sym in r1000, sym in r3000
        old1, old3 = bool(r['in_r1000']), bool(r['in_r3000'])
        if (new1, new3) == (old1, old3):
            continue
        changes.append((sym, new1, new3))
        t = types.get(sym) or 'unknown'
        if new1 != old1:
            n1 += 1
            (enter if new1 else leave)['r1000'][t] += 1
        if new3 != old3:
            n3 += 1
            (enter if new3 else leave)['r3000'][t] += 1
    return {'rows': len(rows), 'changes': changes, 'n_r1000': n1, 'n_r3000': n3,
            'leave': leave, 'enter': enter}


def _fmt_counter(c: Counter) -> str:
    return ', '.join(f'{k}={v}' for k, v in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))) or '-'


def format_report(snapshot_date, d: dict) -> str:
    lines = [f"{snapshot_date}: rows={d['rows']} in_r1000_changes={d['n_r1000']} "
             f"in_r3000_changes={d['n_r3000']}"]
    for flag in ('r1000', 'r3000'):
        lines.append(f"  {flag} leave[{sum(d['leave'][flag].values())}]: {_fmt_counter(d['leave'][flag])}")
        lines.append(f"  {flag} enter[{sum(d['enter'][flag].values())}]: {_fmt_counter(d['enter'][flag])}")
    return '\n'.join(lines)


def format_total(per_date: list, apply: bool) -> str:
    tot = {'rows': 0, 'n_r1000': 0, 'n_r3000': 0, 'updated': 0}
    for _, d in per_date:
        tot['rows'] += d['rows']
        tot['n_r1000'] += d['n_r1000']
        tot['n_r3000'] += d['n_r3000']
        tot['updated'] += len(d['changes'])
    out = (f"TOTAL ({'APPLIED' if apply else 'DRY-RUN'}): dates={len(per_date)} rows={tot['rows']} "
           f"in_r1000_changes={tot['n_r1000']} in_r3000_changes={tot['n_r3000']} "
           f"rows_to_update={tot['updated']}")
    if apply:
        out += ("\nREMINDER: rebuild the tier membership artifact "
                "(scripts/build_tier_membership.py) so backtests see the corrected month-ends.")
    return out


def _fetch_dicts(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def run(conn, date_from: date, date_to: date, apply: bool, out=print) -> list:
    """Process every snapshot date in range. Raises on error after rolling the
    failing date back (earlier dates stay committed in --apply mode)."""
    if not apply:
        conn.set_session(readonly=True)
    with conn.cursor() as cur:
        cur.execute(LATEST_SECURITY_TYPE_SQL)
        types = {sym: st for sym, st in cur.fetchall()}
        cur.execute(DATES_SQL, (date_from, date_to))
        dates = [r[0] for r in cur.fetchall()]
    (conn.commit if apply else conn.rollback)()
    per_date = []
    for sd in dates:
        try:
            with conn.cursor() as cur:
                cur.execute(ROWS_SQL, (sd,))
                rows = _fetch_dicts(cur)
                d = diff_flags(rows, types)
                if apply and d['changes']:
                    from psycopg2.extras import execute_batch
                    execute_batch(cur, UPDATE_SQL,
                                  [(n1, n3, sd, sym) for sym, n1, n3 in d['changes']])
            if apply:
                conn.commit()
            else:
                conn.rollback()
        except Exception:
            conn.rollback()
            raise
        out(format_report(sd, d))
        per_date.append((sd, d))
    out(format_total(per_date, apply))
    return per_date


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--from', dest='date_from', required=True, type=date.fromisoformat)
    ap.add_argument('--to', dest='date_to', type=date.fromisoformat, default=None)
    ap.add_argument('--apply', action='store_true', help='write (default: dry-run, read-only)')
    ap.add_argument('--force-range', action='store_true',
                    help=f'allow --from earlier than {MIN_SAFE_FROM}')
    a = ap.parse_args(argv)
    if a.date_from < MIN_SAFE_FROM and not a.force_range:
        ap.error(f'--from {a.date_from} is before {MIN_SAFE_FROM}; older snapshots are clean '
                 '(pass --force-range to override)')
    if a.date_to is None:
        a.date_to = date.max
    if a.date_to < a.date_from:
        ap.error('--to is before --from')
    return a


def main(argv=None, connect=None) -> int:
    args = parse_args(argv)
    try:
        if connect is None:
            import psycopg2
            connect = psycopg2.connect
        conn = connect(os.environ['POSTGRES_URI'])
    except Exception as e:  # noqa: BLE001
        print(f'ERROR: cannot connect: {type(e).__name__}: {e}', file=sys.stderr)
        return 1
    try:
        run(conn, args.date_from, args.date_to, args.apply)
    except Exception as e:  # noqa: BLE001
        print(f'ERROR: {type(e).__name__}: {e}', file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
