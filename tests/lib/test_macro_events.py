"""C3 reader: T-1..T gated sessions over a synthetic macro_events master.

T-1 is the PREVIOUS NYSE SESSION, not the calendar day before — a Monday
release gates the preceding Friday.
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
import pytest

from lib import macro_events as me
from lib import trading_calendar as tc


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    """Sessions Mon-Fri 2026-08-01..2026-10-31, minus Labor Day 2026-09-07.

    Starts a month before the query window (not 2026-09-01) so that
    trading_calendar._sessions_any's `cal.covers(start) and cal.covers(end)`
    check succeeds for prev_session's 14-day lookback even when the release
    itself sits near the front of the window (see 2026-09-08 below) — with a
    flush-to-the-query-window calendar, prev_session's lookback would spill
    past cal.first and trip the `no alpaca probe` guard below. Deviation from
    the brief's fixture (which started 2026-09-01); noted in the task-8
    report."""
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-08-01', '2026-10-31')
            if d.date() != dt.date(2026, 9, 7)]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions',
                        lambda a, b: (_ for _ in ()).throw(AssertionError('no alpaca probe')))
    yield
    tc.clear_cache()


def _master(tmp_path, monkeypatch, rows):
    df = pd.DataFrame(rows, columns=['event', 'scheduled_at', 'session_date',
                                     'source', 'ingested_at'])
    p = tmp_path / 'macro_events.parquet'
    df.to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    return p


TS = pd.Timestamp('2026-09-01T12:00:00Z')


def _row(event, session, hour_utc=12):
    d = session
    return {'event': event,
            'scheduled_at': pd.Timestamp(dt.datetime(d.year, d.month, d.day, hour_utc),
                                         tz='UTC'),
            'session_date': d, 'source': 'test', 'ingested_at': TS}


def test_missing_master_is_inert(tmp_path, monkeypatch):
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(tmp_path / 'nope.parquet'))
    assert me.load_events() == []
    assert me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30)) == {}
    assert me.gating_event(dt.date(2026, 9, 16)) is None


def test_load_events_filters_to_high_importance(tmp_path, monkeypatch, calendar):
    _master(tmp_path, monkeypatch, [
        _row('CPI', dt.date(2026, 9, 16)),
        _row('PCE', dt.date(2026, 9, 25)),
        _row('FOMC_MINUTES', dt.date(2026, 10, 7)),
    ])
    assert [r['event'] for r in me.load_events()] == ['CPI']
    assert len(me.load_events(events=me.EVENTS)) == 3


def test_gated_sessions_covers_t_minus_one_and_t(tmp_path, monkeypatch, calendar):
    _master(tmp_path, monkeypatch, [_row('CPI', dt.date(2026, 9, 16))])
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert got == {dt.date(2026, 9, 15): ['CPI'], dt.date(2026, 9, 16): ['CPI']}


def test_t_minus_one_skips_a_holiday_and_the_weekend(tmp_path, monkeypatch, calendar):
    # 2026-09-08 is the Tuesday after Labor Day; its previous session is Fri 09-04.
    _master(tmp_path, monkeypatch, [_row('NFP', dt.date(2026, 9, 8))])
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert set(got) == {dt.date(2026, 9, 4), dt.date(2026, 9, 8)}


def test_two_events_on_one_session_are_merged_and_sorted(tmp_path, monkeypatch, calendar):
    _master(tmp_path, monkeypatch, [_row('CPI', dt.date(2026, 9, 16)),
                                    _row('FOMC_DECISION', dt.date(2026, 9, 17))])
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert got[dt.date(2026, 9, 16)] == ['CPI', 'FOMC_DECISION']


def test_window_bounds_are_inclusive(tmp_path, monkeypatch, calendar):
    _master(tmp_path, monkeypatch, [_row('CPI', dt.date(2026, 9, 16))])
    assert me.gated_sessions(dt.date(2026, 9, 16), dt.date(2026, 9, 16)) == {
        dt.date(2026, 9, 16): ['CPI']}
    assert me.gated_sessions(dt.date(2026, 10, 1), dt.date(2026, 10, 31)) == {}


def test_gating_event_renders_the_shadow_line_token(tmp_path, monkeypatch, calendar):
    _master(tmp_path, monkeypatch, [_row('CPI', dt.date(2026, 9, 16)),
                                    _row('FOMC_DECISION', dt.date(2026, 9, 17))])
    assert me.gating_event(dt.date(2026, 9, 16)) == \
        'CPI@2026-09-16,FOMC_DECISION@2026-09-17'
    assert me.gating_event(dt.date(2026, 9, 14)) is None


def test_unreadable_master_is_inert_not_fatal(tmp_path, monkeypatch):
    p = tmp_path / 'macro_events.parquet'
    p.write_bytes(b'not a parquet')
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    assert me.load_events() == []


def test_t_minus_one_logs_at_error_on_a_calendar_failure(monkeypatch, caplog):
    """Fix round 1 item 4: the calendar's own weekday fallback already
    degrades silently — _t_minus_one's except must not compound that by
    being quiet too. ERROR, not WARNING (T8 review + T10)."""
    def _boom(_session):
        raise RuntimeError('calendar unreadable')

    monkeypatch.setattr('lib.trading_calendar.prev_session', _boom)
    with caplog.at_level(logging.INFO, logger=me.log.name):
        result = me._t_minus_one(dt.date(2026, 9, 16))
    assert result is None  # fail-open: T-1 is skipped, not fatal
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, 'expected an ERROR record on prev_session() failure'
    assert '[macro_events]' in error_records[-1].getMessage()


# ── T8 fix-round-1 item 1: active=False correctability ──────────────────────

def _master_with_active(tmp_path, monkeypatch, rows):
    df = pd.DataFrame(rows, columns=['event', 'scheduled_at', 'session_date',
                                     'source', 'ingested_at', 'active'])
    p = tmp_path / 'macro_events.parquet'
    df.to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    return p


def test_reader_ignores_an_active_false_row(tmp_path, monkeypatch, calendar):
    rows = [{**_row('CPI', dt.date(2026, 9, 16)), 'active': False},
            {**_row('FOMC_DECISION', dt.date(2026, 9, 17)), 'active': True},
            {**_row('NFP', dt.date(2026, 9, 4)), 'active': None}]  # NULL == active
    _master_with_active(tmp_path, monkeypatch, rows)
    got = sorted(r['event'] for r in me.load_events(events=me.EVENTS))
    assert got == ['FOMC_DECISION', 'NFP']


def test_reader_gates_normally_when_active_column_is_missing(tmp_path, monkeypatch, calendar):
    """Regression: probing for 'active' before projecting it must not raise
    on a master written before this column existed (the pre-existing
    _master() fixture below writes exactly 5 columns, no 'active')."""
    _master(tmp_path, monkeypatch, [_row('CPI', dt.date(2026, 9, 16))])
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert got == {dt.date(2026, 9, 15): ['CPI'], dt.date(2026, 9, 16): ['CPI']}
