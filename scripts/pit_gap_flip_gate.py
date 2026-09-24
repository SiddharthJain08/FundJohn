#!/usr/bin/env python3
"""pit_gap_flip_gate.py — read-only gate check for the OPENCLAW_FINANCIALS_PIT=1
+ OPENCLAW_BT_GAP_FILL=open live flip (spec 2026-09-12 §A4).

Prints 'OK' or 'NOT_YET' on the first line, then the per-gate detail — the same
contract scripts/target_mode_flip_after_fleet.sh parses.

  G1 fleet uniformity: every manifest state=live strategy's LATEST primary
     backtest row carries BOTH config_json.gap_fill='open' and
     config_json.financials_pit=true (at most --max-lagging exceptions, default
     3 — a strategy whose backtest OOMs every night must not hold the flip
     hostage; exceptions are listed).

There is deliberately NO Sharpe gate here, unlike scripts/target_mode_flip_gate.py.
Operator ruling R1 (2026-09-12) accepts up front that point-in-time fundamentals
"may lower the fundamentals sleeve's Sharpes and demote strategies", so a
median-ΔSharpe leg would block a flip that was approved on exactly those terms;
and there is no clean baseline population to compare against, because the prior
rows are a mix of target_mode 'flat' and 'atr_r'. Do not add one back.

Usage: python3 scripts/pit_gap_flip_gate.py [--max-lagging N] [--env-file PATH]
                                            [--manifest PATH] [--rows-json PATH]
--rows-json is the test seam: a JSON list of
[strategy_id, gap_fill, financials_pit, run_at_iso] used INSTEAD of the DB.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys


def verdict(live, rows, max_lagging: int):
    """(ok, detail_lines). `rows` is newest-first-agnostic: the newest run_at
    per strategy wins. A live strategy with no row at all lags."""
    latest: dict = {}
    for sid, gap_fill, fin_pit, run_at in rows:
        if sid not in live:
            continue
        prev = latest.get(sid)
        if prev is None or str(run_at) > str(prev[2]):
            latest[sid] = (gap_fill, bool(fin_pit), run_at)
    lagging = []
    for sid in live:
        row = latest.get(sid)
        if row is None or row[0] != 'open' or row[1] is not True:
            lagging.append(sid)
    ok = len(lagging) <= max_lagging
    detail = [f"  G1 fleet: live={len(live)} on_new={len(live) - len(lagging)} "
              f"lagging={len(lagging)} (max {max_lagging}) -> {'OK' if ok else 'NOT_YET'}"]
    if lagging:
        detail.append('    lagging: ' + ', '.join(lagging[:12]) + (' …' if len(lagging) > 12 else ''))
    detail.append('  G2 sharpe: intentionally absent — ruling R1 accepts PIT-driven '
                  'Sharpe reductions and demotions')
    return ok, detail


def _rows_from_db(uri):
    import psycopg2
    with psycopg2.connect(uri) as c, c.cursor() as cur:
        cur.execute("""SELECT strategy_id,
                              COALESCE(config_json->>'gap_fill', 'level'),
                              COALESCE((config_json->>'financials_pit')::boolean, false),
                              run_at
                         FROM strategy_backtest_runs
                        WHERE primary_window = true
                        ORDER BY strategy_id, run_at DESC""")
        return [[r[0], r[1], bool(r[2]), r[3].isoformat() if hasattr(r[3], 'isoformat') else str(r[3])]
                for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-lagging', type=int, default=3)
    ap.add_argument('--env-file', default='/root/openclaw/.env')
    ap.add_argument('--manifest', default='/root/openclaw/src/strategies/manifest.json')
    ap.add_argument('--rows-json', default=None)
    a = ap.parse_args()
    live = sorted(k for k, v in json.load(open(a.manifest))['strategies'].items()
                  if v.get('state') == 'live')
    if a.rows_json:
        rows = json.load(open(a.rows_json))
    else:
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            m = re.search(r'^POSTGRES_URI=(.*)$', open(a.env_file).read(), re.M)
            uri = m.group(1).strip().strip('"') if m else None
        if not uri:
            print('NOT_YET'); print('  POSTGRES_URI not found'); return 0
        rows = _rows_from_db(uri)
    ok, detail = verdict(live, rows, a.max_lagging)
    print('OK' if ok else 'NOT_YET')
    print('\n'.join(detail))
    return 0


if __name__ == '__main__':
    sys.exit(main())
