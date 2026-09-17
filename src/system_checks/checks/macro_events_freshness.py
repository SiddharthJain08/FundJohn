"""Storage-tagged check: the macro-event master still knows about the FUTURE.

master_freshness asks "was this file written recently". For a calendar master
that is the wrong question: a monthly ingest that starts 403-ing leaves mtime
and max(ingested_at) looking healthy for weeks while the C3 T-1..T entry gate
quietly stops gating anything (gated_sessions() returns {} and the sizer sails
straight through every CPI print). So this check asserts FORWARD coverage —
a high-importance release at least MIN_HORIZON_DAYS ahead, and at least one
future row for each of FOMC_DECISION / CPI / NFP.

macro_events.parquet is therefore listed in master_freshness._COVERED_ELSEWHERE
rather than its _CADENCES table.
"""
from __future__ import annotations

import datetime as dt

from ..registry import check
from ..types import Status

MIN_HORIZON_DAYS = 30


@check(name='macro_events_fresh', tags=['storage'], requires=[])
def _macro_events_fresh():
    from lib.macro_events import HIGH_IMPORTANCE, load_events, master_path

    p = master_path()
    if not p.exists():
        return Status.WARN, f'macro_events master missing at {p} (C3 gate inert)'

    rows = load_events(events=HIGH_IMPORTANCE)
    if not rows:
        return Status.FAIL, f'macro_events at {p}: no high-importance rows'

    today = dt.date.today()
    horizon = max(r['session_date'] for r in rows)
    days = (horizon - today).days
    if days < MIN_HORIZON_DAYS:
        return Status.FAIL, (f'macro_events forward coverage {days}d '
                             f'(min {MIN_HORIZON_DAYS}d); last event {horizon}')

    counts: dict = {}
    for r in rows:
        if r['session_date'] >= today:
            counts[r['event']] = counts.get(r['event'], 0) + 1
    missing = [e for e in HIGH_IMPORTANCE if not counts.get(e)]
    if missing:
        return Status.WARN, (f'macro_events {days}d ahead but no future '
                             f'{",".join(missing)} rows (have {counts})')
    return Status.PASS, f'macro_events covers {days}d ahead ({counts})'
