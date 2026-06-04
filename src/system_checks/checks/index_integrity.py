"""Storage check: btree index integrity on trading-critical tables (amcheck).

Born 2026-06-04 (LRN-20260604-003): 4 of 31 btrees on critical tables were
corrupt — rows silently fell OUT of the indexes (~04-25, OOM-era interrupted
write), making them invisible to index-plan reads while ON CONFLICT upserts
double-inserted byte-identical PK twins. Sat undetected for six weeks because
nothing ever asked the indexes if they were sane.

bt_index_check(idx, heapallindexed=true) catches BOTH failure modes: structural
invariant violations AND heap rows missing from the index. ALERT-ONLY by design
— repair (dedupe + REINDEX) deletes canonical rows and stays operator-gated.
"""
from __future__ import annotations

import os

import psycopg2

from ..registry import check
from ..types import Status

# Tables whose btrees guard live trading correctness. Same scope as the
# 2026-06-04 sweep that found the corruption.
CRITICAL_TABLES = (
    'data_coverage', 'universe_config', 'execution_signals',
    'strategy_weights_by_regime', 'alpaca_submissions',
    'regime_sizer_params', 'signal_pnl', 'strategy_registry',
    'eod_compute_health',
)


@check(name='btree_index_integrity', tags=['storage'], requires=['db'])
def _btree_index_integrity():
    """amcheck every btree on the critical tables; FAIL names the corrupt ones."""
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL', '')
    if not uri:
        return Status.FAIL, 'POSTGRES_URI not set'
    try:
        conn = psycopg2.connect(uri)
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute('CREATE EXTENSION IF NOT EXISTS amcheck')
        except Exception:
            return Status.SKIP, 'amcheck extension unavailable (no privilege?)'
        # Bound the sweep so a pathological index can't wedge maintenance.
        cur.execute("SET statement_timeout = '120s'")
        cur.execute(
            """SELECT c.relname FROM pg_index i
               JOIN pg_class c ON c.oid = i.indexrelid
               JOIN pg_class t ON t.oid = i.indrelid
               JOIN pg_am am ON am.oid = c.relam
               WHERE am.amname = 'btree' AND t.relname = ANY(%s)
               ORDER BY c.relname""",
            (list(CRITICAL_TABLES),),
        )
        indexes = [r[0] for r in cur.fetchall()]
        corrupt = []
        for ix in indexes:
            try:
                cur.execute('SELECT bt_index_check(%s::regclass, true)', (ix,))
            except Exception as e:
                corrupt.append(f'{ix} ({str(e).splitlines()[0][:60]})')
        conn.close()
        if corrupt:
            return Status.FAIL, (
                f'{len(corrupt)}/{len(indexes)} corrupt btree(s): {corrupt[:4]} — '
                'do NOT auto-repair; see LRN-20260604-003 + '
                '/root/db_corruption_repair_2026-06-04.log for the procedure')
        return Status.PASS, f'all {len(indexes)} btrees on {len(CRITICAL_TABLES)} critical tables clean (heapallindexed)'
    except Exception as e:
        return Status.ERROR, f'amcheck sweep failed: {e}'
