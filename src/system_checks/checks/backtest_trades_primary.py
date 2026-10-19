"""Storage-tagged check: strategy_backtest_trades_primary mirrors the base table.

The hot table (migration 166) is a cache of strategy_backtest_trades rows for
primary-window runs. Row counts per primary run must match, no hot row may
reference a missing/non-primary run, and hot strategy_id must equal the run's.
Repair: python3 -m backtest.trades_primary --resync-drifted
"""
from __future__ import annotations

import os

from ..registry import check
from ..types import Status

_REPAIR = 'repair: python3 -m backtest.trades_primary --resync-drifted'


def evaluate(*, table_exists: bool, count_mismatches: list, orphan_runs: int,
             id_mismatches: int, n_primary_runs: int):
    if not table_exists:
        return Status.WARN, 'strategy_backtest_trades_primary absent — migration 166 not applied'
    if count_mismatches or orphan_runs or id_mismatches:
        sids = []
        for m in count_mismatches:
            if m[1] not in sids:
                sids.append(m[1])
        d = (f'{len(count_mismatches)} run count drift, {orphan_runs} orphan runs, '
             f'{id_mismatches} id mismatch; {",".join(s[:24] for s in sids[:3])}; {_REPAIR}')
        return Status.FAIL, d[:199]
    return Status.PASS, f'{n_primary_runs} primary runs in sync'


@check(name='backtest_trades_primary_sync', tags=['storage'], requires=['db'])
def _backtest_trades_primary_sync():
    # Lazy imports: a module-level data/backtest import broke the registry once.
    import psycopg2
    from backtest.trades_primary import drift_report, hot_table_exists
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL', '')
    if not uri:
        return Status.SKIP, 'POSTGRES_URI not set'
    conn = psycopg2.connect(uri, connect_timeout=5)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET statement_timeout='60s'")
        if not hot_table_exists(cur):
            return evaluate(table_exists=False, count_mismatches=[], orphan_runs=0,
                            id_mismatches=0, n_primary_runs=0)
        mism, orphans, idm, n = drift_report(cur)
        return evaluate(table_exists=True, count_mismatches=mism, orphan_runs=orphans,
                        id_mismatches=idm, n_primary_runs=n)
    finally:
        conn.close()
