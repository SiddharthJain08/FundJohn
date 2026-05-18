"""Tradable-universe freshness checks.

The trade_handoff_builder reads `alpaca_tradable_universe` to drop signals on
delisted/halted/unsupported tickers before submission. If the table is empty
or stale, we'd silently fall back to the fail-open path and start eating
HTTP 422 'asset not active' rejections again.

These checks surface that staleness so BotJohn's daily maintenance digest
flags it before the operator notices via wasted submissions.
"""
from __future__ import annotations

import os

from ..registry import check
from ..types import Status


def _connect():
    import psycopg2
    return psycopg2.connect(os.environ['POSTGRES_URI'])


@check(name='universe_table_populated', tags=['universe', 'broker'], requires=['db'])
def _universe_table_populated():
    """Universe table must have at least 5,000 rows. Below that signals an
    auth failure or aborted refresh — handoff filter will fail-open."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM alpaca_tradable_universe WHERE tradable = TRUE AND status = 'active'")
    n = cur.fetchone()[0]
    conn.close()
    if n == 0:
        return Status.FAIL, 'table empty — refresh has never run successfully'
    if n < 5000:
        return Status.WARN, f'only {n} tradable rows (expected ~12k); refresh may have aborted'
    return Status.PASS, f'{n:,} tradable symbols loaded'


@check(name='universe_freshness', tags=['universe', 'broker'], requires=['db'])
def _universe_freshness():
    """Last successful refresh must be within 36 hours (covers weekends).
    Soft-fail at 30h (WARN), hard-fail at 48h (FAIL)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """SELECT EXTRACT(EPOCH FROM (NOW() - MAX(run_at))) / 3600.0
           FROM alpaca_tradable_universe_refresh_log"""
    )
    row = cur.fetchone()
    conn.close()
    if not row or row[0] is None:
        return Status.FAIL, 'no refresh_log rows — refresh has never run'
    hours = float(row[0])
    if hours > 48:
        return Status.FAIL, f'last refresh {hours:.1f}h ago (>48h) — filter is operating on stale data'
    if hours > 30:
        return Status.WARN, f'last refresh {hours:.1f}h ago — schedule slip; re-run refresh_tradable_universe'
    return Status.PASS, f'refreshed {hours:.1f}h ago'


@check(name='universe_recent_diff', tags=['universe', 'broker'], requires=['db'])
def _universe_recent_diff():
    """Surface the latest refresh's add/drop counts so the digest carries
    the operationally interesting signal (delistings worth knowing about)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """SELECT total_active, total_tradable, newly_listed, newly_inactive,
                  status_changes, run_at
           FROM alpaca_tradable_universe_refresh_log
           ORDER BY run_at DESC LIMIT 1"""
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return Status.SKIP, 'no refresh log yet'
    total_active, total_tradable, newly_listed, newly_inactive, status_changes, run_at = row
    summary = (f'last run {run_at:%Y-%m-%d %H:%M UTC}: '
               f'{total_active:,} active, {total_tradable:,} tradable, '
               f'+{newly_listed} listed / -{newly_inactive} inactive / {status_changes} flips')
    # The diff itself is informational — WARN only on suspiciously-large daily drop
    # which usually indicates a CLI auth issue manifesting as a full re-sync.
    if newly_inactive > 200:
        return Status.WARN, f'{newly_inactive} symbols newly inactive — verify Alpaca pull integrity. {summary}'
    return Status.PASS, summary
