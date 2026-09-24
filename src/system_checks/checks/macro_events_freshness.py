"""Storage-tagged check: the macro-event master still knows about the FUTURE.

master_freshness asks "was this file written recently". For a calendar master
that is the wrong question: a monthly ingest that starts 403-ing leaves mtime
and max(ingested_at) looking healthy for weeks while the C3 T-1..T entry gate
quietly stops gating anything (gated_sessions() returns {} and the sizer sails
straight through every CPI print). So this check asserts FORWARD coverage —
a high-importance release at least MIN_HORIZON_DAYS ahead, and at least one
future row for each of FOMC_DECISION / CPI / NFP — plus a bounded read of
`ingested_at` to catch an ingest that stopped running while the forward
window it already wrote is still technically in the future.

macro_events.parquet is therefore listed in master_freshness._COVERED_ELSEWHERE
rather than its _CADENCES table.

Precedence (first match wins): master absent -> SKIP (not yet ingested, not a
failure); master unreadable -> FAIL; zero high-importance rows -> FAIL; short
forward coverage -> FAIL; an event type with no future row -> WARN; stale
ingested_at -> WARN; else PASS.

`load_events()` swallows pyarrow errors and returns [] for both "genuinely
empty" and "unreadable" masters (by design — a gate must fail inert, never
fatal). This check can't reuse that leniency: it needs to tell an ingest that
wrote zero rows apart from a corrupt file, so it does its own guarded
existence+schema probe first (mirroring load_events's own probe pattern) and
only calls load_events() once that probe has proven the file parses.
"""
from __future__ import annotations

import datetime as dt

from ..registry import check
from ..types import Status

MIN_HORIZON_DAYS = 30
# Ingest cadence is monthly (~31d); +14d grace for a missed run before a
# still-in-the-future forward window is treated as evidence the ingest itself
# has stopped running.
MAX_INGEST_AGE_DAYS = 31 + 14


@check(name='macro_events_fresh', tags=['storage'], requires=[])
def _macro_events_fresh():
    from lib.macro_events import HIGH_IMPORTANCE, load_events, master_path

    p = master_path()
    if not p.exists():
        return Status.SKIP, (f'macro_events master absent at {p} — not yet '
                             f'ingested; run the Task 8 two-pass dry-run then write')

    import pyarrow.parquet as pq

    try:
        schema_names = set(pq.ParquetFile(p).schema_arrow.names)
    except Exception as e:  # noqa: BLE001 — corrupt/unreadable file, not empty
        return Status.FAIL, f'macro_events master unreadable: {type(e).__name__}'

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

    ingest_note = ''
    if 'ingested_at' not in schema_names:
        ingest_note = ' (ingested_at column absent — recency unchecked)'
    else:
        try:
            import pandas as pd
            col = pq.read_table(p, columns=['ingested_at']).column('ingested_at')
            max_ingested = pd.to_datetime(col.to_pandas(), utc=True).max()
            if pd.isna(max_ingested):
                ingest_note = ' (ingested_at column empty — recency unchecked)'
            else:
                age_days = (pd.Timestamp.now(tz='UTC') - max_ingested).days
                if age_days > MAX_INGEST_AGE_DAYS:
                    return Status.WARN, (
                        f'macro_events {days}d ahead but ingested_at is '
                        f'{age_days}d old (max {MAX_INGEST_AGE_DAYS}d); last '
                        f'ingest {max_ingested.date()}')
        except Exception as e:  # noqa: BLE001 — this sub-check is optional;
            # the schema probe above already proved the file parses, so a
            # failure here is not "unreadable" — degrade like column-absent.
            ingest_note = f' (ingested_at unreadable: {type(e).__name__} — recency unchecked)'

    return Status.PASS, f'macro_events covers {days}d ahead ({counts}){ingest_note}'
