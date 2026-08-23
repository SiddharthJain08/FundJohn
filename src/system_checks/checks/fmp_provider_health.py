"""Pipeline-tagged check: FMP provider health over the last 24h.

data_provider_health gained FMP rows on 2026-08-23 (collector fundamentals /
insider walk, the generated tool module behind backfillers + sub-agents, the
insider stream, intraday financials, quotes, profiles, universe metadata).
Before that FMP was the one live provider the dashboard tile never saw — the
collector's fundamentals phase halted at ticker ~30 for ten days and nothing
noticed.

Two shapes:
  * error-ratio spike — quota (402), 429, 5xx, transport. Tier-gated symbols
    and 404s are recorded as SUCCESS upstream (the provider answered), so they
    never trip this.
  * weekday silence — zero FMP rows in the window after the 16:15 ET collect
    should have run. Weekends / early-morning are quiet by design.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from ..registry import check
from ..types import Status

WINDOW_HOURS = 24
MIN_CALLS_FOR_RATIO = 20
WARN_ERROR_RATIO = 0.25
FAIL_ERROR_RATIO = 0.75
# Weekday silence only counts once the day's collect has had time to run:
# 16:15 ET ≈ 20:15 UTC (EDT) / 21:15 UTC (EST); judge from 21:30 UTC.
SILENCE_AFTER_UTC_HOUR = 21


def evaluate(rows, *, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    ok = sum(int(r.get('success_count') or 0) for r in rows)
    err = sum(int(r.get('error_count') or 0) for r in rows)
    total = ok + err
    if total == 0:
        weekday = now.weekday() < 5
        if weekday and now.hour >= SILENCE_AFTER_UTC_HOUR:
            return Status.WARN, f'no FMP calls recorded in the last {WINDOW_HOURS}h on a weekday — collector fundamentals/insider phases silent?'
        return Status.PASS, f'no FMP calls in the last {WINDOW_HOURS}h (quiet period)'
    ratio = err / total
    worst = max(rows, key=lambda r: int(r.get('error_count') or 0))
    last = (worst.get('last_error') or '')[:80]
    summary = f'{ok} ok / {err} err across {len(rows)} endpoint(s), error ratio {ratio * 100:.1f}%'
    if total >= MIN_CALLS_FOR_RATIO and ratio >= FAIL_ERROR_RATIO:
        return Status.FAIL, f'{summary} — worst {worst.get("endpoint")}: {last}'
    if total >= MIN_CALLS_FOR_RATIO and ratio >= WARN_ERROR_RATIO:
        return Status.WARN, f'{summary} — worst {worst.get("endpoint")}: {last}'
    return Status.PASS, summary


@check(name='fmp_provider_health', tags=['pipeline'], requires=['db'])
def _fmp_provider_health():
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return Status.SKIP, 'POSTGRES_URI not set'
    import psycopg2
    with psycopg2.connect(uri, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT endpoint, SUM(success_count), SUM(error_count),
                   (ARRAY_AGG(last_error ORDER BY last_error_at DESC NULLS LAST))[1]
              FROM data_provider_health
             WHERE provider = 'fmp' AND window_start >= NOW() - (%s || ' hours')::interval
             GROUP BY endpoint
            """, (str(WINDOW_HOURS),))
        rows = [{'endpoint': e, 'success_count': s, 'error_count': x, 'last_error': le}
                for e, s, x, le in cur.fetchall()]
    return evaluate(rows)
