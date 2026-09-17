"""Macro-event calendar reader (spec 2026-09-12 §3 C3, operator ruling R3).

Master: data/master/macro_events.parquet, built by
src/ingestion/ingest_macro_events.py. Append-only, dedup key
(event, scheduled_at). Columns:

    event         FOMC_DECISION | CPI | NFP | PCE | GDP_ADV | FOMC_MINUTES
    scheduled_at  UTC timestamp of the release
    session_date  the NYSE session the release lands in (lib.trading_calendar)
    source        'federalreserve.gov' | 'bls.gov' | 'bea.gov'
    ingested_at   UTC timestamp of the fetch
    active        bool, nullable. False = correctable-without-DELETE marker
                  written by `ingest_macro_events.py --deactivate` (fix
                  round 1, T8 item 1). A parsed row NEVER writes this column
                  explicitly — it stays NULL — so the merge-time rule "never
                  flip active back to True unless the incoming row carries
                  True explicitly" (see ingest_macro_events.merge_into_master)
                  means a routine re-ingest can never silently resurrect a
                  deactivated event. MISSING column or NULL both count as
                  active here; only an explicit False is excluded.

Every reader here is INERT (empty result + a warning) when the master is
missing or unreadable: a gate that cannot read its calendar must not block
trading. The file is ~1k rows, so a column-projected read is well inside the
8 GB box budget.

T-1 means the PREVIOUS NYSE SESSION, not the calendar day before — a Monday
release gates the preceding Friday, and a post-holiday release gates the
session before the holiday.

PARSERS FEEDING THIS MASTER ARE UNVALIDATED against a live fetch as of this
module's authoring (see src/ingestion/ingest_macro_events.py docstring) —
the operator validation gate (plan Step 12) must pass before the master is
backfilled and any consumer (Task 10's sizer gate) is armed against it.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
MASTER_PATH_ENV = 'OPENCLAW_MACRO_EVENTS_PATH'
DEFAULT_MASTER = ROOT / 'data' / 'master' / 'macro_events.parquet'

EVENTS = ('FOMC_DECISION', 'CPI', 'NFP', 'PCE', 'GDP_ADV', 'FOMC_MINUTES')
# Ruling R3 scopes the entry block to these three.
HIGH_IMPORTANCE = ('FOMC_DECISION', 'CPI', 'NFP')
# NOTE: ingest_macro_events.py holds its own copy of this list (not a shared
# import) — the two must be changed together. See T8 fix-round-1 item 1.
COLUMNS = ['event', 'scheduled_at', 'session_date', 'source', 'ingested_at',
          'active']


def master_path() -> Path:
    return Path(os.environ.get(MASTER_PATH_ENV) or DEFAULT_MASTER)


def _as_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def load_events(events=HIGH_IMPORTANCE) -> list:
    """[{'event', 'session_date'}] sorted by (session_date, event). [] when the
    master is absent or unreadable — the gate is then inert, never fatal.

    Rows with active is False are skipped; a MISSING 'active' column (a
    master written before this fix round) or a NULL value both count as
    active. `pd.read_parquet(path, columns=[...])` RAISES if a requested
    column is absent, so 'active' is only added to the projection after a
    cheap schema probe confirms it exists — projecting it unconditionally
    would turn "master predates this column" into an unreadable master and
    silently make the whole gate inert (the exact failure mode this file's
    module docstring warns against)."""
    p = master_path()
    if not p.exists():
        log.warning('[macro_events] master missing at %s; gate inert', p)
        return []
    try:
        import pandas as pd
        import pyarrow.parquet as pq
        cols = ['event', 'session_date']
        try:
            if 'active' in set(pq.ParquetFile(p).schema_arrow.names):
                cols.append('active')
        except Exception:  # noqa: BLE001 — schema probe failure: read without
            # 'active' rather than give up (a deactivation gets ignored, but
            # the gate itself keeps working — the inert-gate failure mode is
            # worse than a missed deactivation).
            pass
        df = pd.read_parquet(p, columns=cols)
    except Exception as e:  # noqa: BLE001
        log.warning('[macro_events] master unreadable (%s: %s); gate inert',
                    type(e).__name__, e)
        return []
    wanted = set(events or ())
    active_col = df['active'] if 'active' in df.columns else None
    out = []
    for i, (ev, sd) in enumerate(zip(df.get('event', []), df.get('session_date', []))):
        if active_col is not None:
            a = active_col.iloc[i]
            if pd.notna(a) and not bool(a):
                continue
        if wanted and str(ev) not in wanted:
            continue
        d = _as_date(sd)
        if d is not None:
            out.append({'event': str(ev), 'session_date': d})
    return sorted(out, key=lambda r: (r['session_date'], r['event']))


def _t_minus_one(session):
    from lib.trading_calendar import prev_session
    try:
        return prev_session(session)
    except Exception as e:  # noqa: BLE001
        # ERROR, not WARNING (T8 review + T10): the calendar's own weekday
        # fallback already degrades silently — this except must not compound
        # that by being quiet too. A caller of gating_event/gated_sessions
        # still gets a usable (T-only) result; it is the operator who needs
        # to hear about the degradation loudly.
        log.error('[macro_events] prev_session(%s) failed (%s); T-1 skipped',
                  session, e)
        return None


def gated_sessions(start, end, events=HIGH_IMPORTANCE) -> dict:
    """{session_date: [event, ...]} for every session in [start, end] that is
    T-1 or T of a listed release. Bounds are inclusive; either may be None."""
    out: dict = {}
    for r in load_events(events=events):
        t = r['session_date']
        for d in (t, _t_minus_one(t)):
            if d is None:
                continue
            if start is not None and d < start:
                continue
            if end is not None and d > end:
                continue
            bucket = out.setdefault(d, [])
            if r['event'] not in bucket:
                bucket.append(r['event'])
    for d in out:
        out[d].sort()
    return out


def gating_event(session, events=HIGH_IMPORTANCE):
    """'CPI@2026-09-16,FOMC_DECISION@2026-09-17' when `session` is T-1 or T of
    at least one listed release, else None. The string is the `events=` token
    of the [event_gate] line, so it is sorted and comma-joined."""
    hits = []
    for r in load_events(events=events):
        t = r['session_date']
        if t == session or _t_minus_one(t) == session:
            hits.append(f"{r['event']}@{t.isoformat()}")
    return ','.join(sorted(set(hits))) if hits else None
