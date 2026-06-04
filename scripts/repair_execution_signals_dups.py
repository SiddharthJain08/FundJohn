#!/usr/bin/env python3
"""Repair execution_signals corruption twins + REINDEX the broken unique key.

Background (2026-06-04, sp6 diagnosis §12 / LRN-20260604-003): the btree
behind execution_signals_strategy_id_signal_date_ticker_direction_key lost
entries (~04-25 OOM-era), so the engine's ON CONFLICT double-inserted 97
byte-identical (strategy_id, signal_date, ticker, direction) tuples on the
high-churn days 05-13/20/22 — and signal_pnl tracked BOTH twins (duplicate
all-zero mark series). REINDEX of a unique index fails while dups exist, so:

  per group: keeper = twin with greatest (max child pnl_date, n_children),
             tiebreak oldest created_at (the original row);
             DELETE the dead twin's signal_pnl rows + the dead twin itself
             (corruption artifacts — operator-approved exception to the
             append-only rule; every deleted row is logged in full).
  then:      REINDEX the unique key; bt_index_check verify; Discord summary.

Usage:
  python3 scripts/repair_execution_signals_dups.py            # DRY-RUN (default)
  python3 scripts/repair_execution_signals_dups.py --execute  # do it

Exit codes: 0 ok / 1 repair error / 2 post-repair verification failed.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_LOG = '/root/db_corruption_repair_2026-06-04.log'
UNIQUE_KEY = 'execution_signals_strategy_id_signal_date_ticker_direction_key'


def _load_env():
    for line in open(ROOT / '.env'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        os.environ.setdefault(k.strip(), v.strip())


def pick_keeper(twins: list[dict]) -> dict:
    """Keeper = actively-marked twin: max (max_pnl_date, n_children); tiebreak
    oldest created_at (the original; the later insert is the corruption)."""
    def rank(t):
        return (t['max_pnl_date'] or datetime.date.min, t['n_children'])
    best = max(rank(t) for t in twins)
    candidates = [t for t in twins if rank(t) == best]
    return min(candidates, key=lambda t: t['created_at'])


def _post_discord(text: str) -> None:
    try:
        import requests
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT webhook_urls->>'data-alerts' FROM agent_registry WHERE id='botjohn'")
                row = cur.fetchone()
        url = row and row[0]
        if url:
            requests.post(url, json={'content': text[:1900]},
                          headers={'User-Agent': 'openclaw-repair/1.0'}, timeout=10)
    except Exception as e:  # alerting must never fail the repair
        print(f'[repair] discord post failed: {e}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--execute', action='store_true',
                    help='actually delete + reindex (default: dry-run)')
    args = ap.parse_args()
    dry = not args.execute

    _load_env()
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    # Ground truth must not depend on the corrupt index.
    cur.execute('SET enable_indexscan=off; SET enable_bitmapscan=off; SET enable_mergejoin=off;')

    audit = open(AUDIT_LOG, 'a')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    hdr = f"=== execution_signals twin repair ({'DRY-RUN' if dry else 'EXECUTE'}) {stamp} UTC ==="
    print(hdr); audit.write('\n' + hdr + '\n')

    cur.execute("""
      SELECT strategy_id, signal_date, ticker, direction,
             array_agg(id::text ORDER BY created_at) AS ids
      FROM execution_signals
      GROUP BY 1, 2, 3, 4 HAVING COUNT(*) > 1""")
    groups = cur.fetchall()
    print(f'dup groups: {len(groups)}')

    deleted_signals = deleted_pnl = skipped = 0
    for g in groups:
        twins = []
        for sid in g['ids']:
            cur.execute("""SELECT MAX(pnl_date) AS mx, COUNT(*) AS n
                           FROM signal_pnl WHERE signal_id = %s""", (sid,))
            st = cur.fetchone()
            cur.execute("SELECT created_at FROM execution_signals WHERE id = %s", (sid,))
            twins.append({'id': sid, 'created_at': cur.fetchone()[0],
                          'max_pnl_date': st['mx'], 'n_children': st['n']})
        keeper = pick_keeper(twins)
        dead = [t for t in twins if t['id'] != keeper['id']]
        if not dead:
            skipped += 1
            continue
        line = (f"GROUP {g['strategy_id']}/{g['signal_date']}/{g['ticker']}/{g['direction']}: "
                f"KEEP {keeper['id']} (marked-through={keeper['max_pnl_date']} n={keeper['n_children']}); "
                f"DELETE {[d['id'] for d in dead]}")
        print(' ', line); audit.write(line + '\n')
        for d in dead:
            cur.execute("SELECT row_to_json(p) FROM signal_pnl p WHERE signal_id = %s", (d['id'],))
            for (rowjson,) in cur.fetchall():
                audit.write(f"  DELETED signal_pnl: {json.dumps(rowjson, default=str)}\n")
            cur.execute("SELECT row_to_json(e) FROM execution_signals e WHERE id = %s", (d['id'],))
            audit.write(f"  DELETED execution_signals: {json.dumps(cur.fetchone()[0], default=str)}\n")
            if not dry:
                cur.execute("DELETE FROM signal_pnl WHERE signal_id = %s", (d['id'],))
                deleted_pnl += cur.rowcount
                cur.execute("DELETE FROM execution_signals WHERE id = %s", (d['id'],))
                deleted_signals += cur.rowcount

    if dry:
        conn.rollback()
        summary = f'DRY-RUN complete: {len(groups)} groups, would delete {len(groups) - skipped} twins. No changes.'
        print(summary); audit.write(summary + '\n'); audit.close()
        return 0

    conn.commit()
    print(f'deleted: {deleted_signals} execution_signals twins, {deleted_pnl} duplicate signal_pnl rows')
    audit.write(f'deleted: {deleted_signals} signals twins, {deleted_pnl} pnl rows\n')

    conn.autocommit = True
    cur.execute(f'REINDEX INDEX {UNIQUE_KEY}')
    print(f'REINDEXED {UNIQUE_KEY}'); audit.write(f'REINDEXED {UNIQUE_KEY}\n')
    try:
        cur.execute('CREATE EXTENSION IF NOT EXISTS amcheck')
        cur.execute('SELECT bt_index_check(%s::regclass, true)', (UNIQUE_KEY,))
        print('amcheck OK'); audit.write('amcheck OK\n')
    except Exception as e:
        msg = f'POST-REPAIR amcheck FAILED: {e}'
        print(msg); audit.write(msg + '\n'); audit.close()
        _post_discord(f'🔴 **execution_signals twin repair: amcheck FAILED after REINDEX** — {e}')
        return 2
    audit.close()
    _post_discord(
        f'✅ **execution_signals twin repair complete** — {deleted_signals} corruption twins + '
        f'{deleted_pnl} duplicate pnl rows removed (audit: {AUDIT_LOG}); `{UNIQUE_KEY}` '
        f'REINDEXed + amcheck green.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
