"""F1 / review I-1: gating_event must cheap-filter on each row's own
session_date before ever calling _t_minus_one (prev_session), and must
memoize _t_minus_one per session_date within a single call — two rows that
release on the same day must not each pay for a separate calendar lookup.

New file (does not touch the existing tests/lib/test_macro_events.py, which
the fix-wave brief requires stay unchanged).
"""
from __future__ import annotations

import datetime as dt

from lib import macro_events as me


def _row(event, session, hour_utc=12):
    return {'event': event,
            'scheduled_at': __import__('pandas').Timestamp(
                dt.datetime(session.year, session.month, session.day, hour_utc),
                tz='UTC'),
            'session_date': session, 'source': 'test',
            'ingested_at': __import__('pandas').Timestamp('2026-01-01T00:00:00Z')}


def _master(tmp_path, monkeypatch, rows):
    pd = __import__('pandas')
    df = pd.DataFrame(rows, columns=['event', 'scheduled_at', 'session_date',
                                     'source', 'ingested_at'])
    p = tmp_path / 'macro_events.parquet'
    df.to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    return p


def test_gating_event_cheap_filters_and_memoizes_prev_session(tmp_path, monkeypatch):
    calls = []
    query = dt.date(2026, 6, 15)

    def _spy(session):
        # `session` here is the ROW's own session_date (_t_minus_one's
        # param name shadows the outer `session` argument) — return the
        # fixed T-1 the same_day row below needs so it actually gates
        # `query`, mirroring a real calendar lookup.
        calls.append(session)
        return query if session == query + dt.timedelta(days=5) else session - dt.timedelta(days=1)

    monkeypatch.setattr('lib.trading_calendar.prev_session', _spy)

    window_start = query
    window_end = query + dt.timedelta(days=14)

    rows = []
    d = query - dt.timedelta(days=200)
    n = 0
    while n < 348:
        if d < window_start or d > window_end:
            rows.append(_row('CPI', d))
            n += 1
        d += dt.timedelta(days=1)

    # Two distinct releases sharing one session_date inside the window (not
    # equal to `query`, so both must go through _t_minus_one) — without
    # memoization this pays for the calendar lookup twice.
    same_day = query + dt.timedelta(days=5)
    rows.append(_row('CPI', same_day))
    rows.append(_row('NFP', same_day))
    assert len(rows) == 350

    _master(tmp_path, monkeypatch, rows)

    result = me.gating_event(query)

    assert len(calls) <= 2, (
        f'expected the 14-day cheap filter + per-session_date memoization to '
        f'hold prev_session calls to <= 2, got {len(calls)}')
    assert result == f'CPI@{same_day.isoformat()},NFP@{same_day.isoformat()}'


def test_gating_event_exact_match_never_calls_prev_session(tmp_path, monkeypatch):
    def _spy(_session):
        raise AssertionError('t == session must short-circuit before _t_minus_one')

    monkeypatch.setattr('lib.trading_calendar.prev_session', _spy)

    query = dt.date(2026, 6, 15)
    _master(tmp_path, monkeypatch, [_row('CPI', query)])

    assert me.gating_event(query) == f'CPI@{query.isoformat()}'
