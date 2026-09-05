# tests/lib/test_trading_calendar.py
from __future__ import annotations
import datetime as dt
import logging
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from lib import trading_calendar as tc

ET = ZoneInfo('America/New_York')


def _write_master(tmp_path, monkeypatch, sessions: list[tuple[str, str, str, bool]]):
    """sessions: (date, open, close, active)."""
    df = pd.DataFrame([{'date': dt.date.fromisoformat(d), 'open': o, 'close': c,
                        'session_open': '0400', 'session_close': '2000',
                        'settlement_date': None, 'active': a, 'source': 'alpaca',
                        'fetched_at': '2026-09-04T00:00:00Z'}
                       for d, o, c, a in sessions])
    p = tmp_path / 'trading_calendar.parquet'
    df.to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    return p


@pytest.fixture
def master(tmp_path, monkeypatch):
    # April 2019 (Good Friday 2019-04-19 is the 3rd Friday), Jan 2025 (day of
    # mourning 2025-01-09), and Sept/Nov 2026 (Labor Day 09-07, early close 11-27).
    rows = []
    for d in pd.bdate_range('2019-04-15', '2019-04-26'):
        if d.date() != dt.date(2019, 4, 19):
            rows.append((d.date().isoformat(), '09:30', '16:00', True))
    for d in pd.bdate_range('2025-01-06', '2025-01-10'):
        rows.append((d.date().isoformat(), '09:30', '16:00', d.date() != dt.date(2025, 1, 9)))
    for d in pd.bdate_range('2026-09-01', '2026-09-11'):
        if d.date() != dt.date(2026, 9, 7):
            rows.append((d.date().isoformat(), '09:30', '16:00', True))
    rows.append(('2026-11-27', '09:30', '13:00', True))
    return _write_master(tmp_path, monkeypatch, rows)


def test_is_session_respects_holidays_and_inactive_rows(master):
    assert tc.is_session('2026-09-04') is True
    assert tc.is_session(dt.date(2026, 9, 7)) is False          # Labor Day
    assert tc.is_session(pd.Timestamp('2026-09-05')) is False   # Saturday
    assert tc.is_session('2025-01-09') is False                 # active=false row


def test_next_prev_session_skip_holiday_weekend(master):
    assert tc.next_session('2026-09-04') == dt.date(2026, 9, 8)
    assert tc.prev_session('2026-09-08') == dt.date(2026, 9, 4)
    assert tc.prev_session(dt.datetime(2026, 9, 8, 15, 0)) == dt.date(2026, 9, 4)


def test_sessions_and_sessions_before(master):
    assert tc.sessions('2026-09-03', '2026-09-09') == [
        dt.date(2026, 9, 3), dt.date(2026, 9, 4), dt.date(2026, 9, 8), dt.date(2026, 9, 9)]
    assert tc.sessions_before('2026-09-09', 2) == [dt.date(2026, 9, 4), dt.date(2026, 9, 8)]


def test_expiry_session_moves_holiday_third_friday_to_thursday(master):
    assert tc.expiry_session(dt.date(2019, 4, 19)) == dt.date(2019, 4, 18)
    assert tc.expiry_session(dt.date(2019, 4, 18)) == dt.date(2019, 4, 18)


def test_is_open_uses_master_close_for_early_close(master):
    assert tc.is_open(dt.datetime(2026, 11, 27, 12, 30, tzinfo=ET)) is True
    assert tc.is_open(dt.datetime(2026, 11, 27, 13, 30, tzinfo=ET)) is False
    assert tc.is_open(dt.datetime(2026, 9, 7, 12, 0, tzinfo=ET)) is False
    assert tc.is_open(dt.datetime(2026, 9, 4, 9, 29, tzinfo=ET)) is False
    assert tc.is_open(dt.datetime(2026, 9, 4, 15, 59, tzinfo=ET)) is True


def test_weekday_fallback_when_master_missing_logs_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(tmp_path / 'absent.parquet'))
    monkeypatch.setattr(tc, '_alpaca_sessions', lambda a, b: None)
    tc.clear_cache()
    with caplog.at_level(logging.WARNING):
        assert tc.is_session('2026-09-07') is True   # weekday fallback cannot see Labor Day
        assert tc.next_session('2026-09-04') == dt.date(2026, 9, 7)
    assert any('weekday fallback' in r.message for r in caplog.records)


def test_alpaca_fallback_used_before_weekday_math(tmp_path, monkeypatch):
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(tmp_path / 'absent.parquet'))
    monkeypatch.setattr(tc, '_alpaca_sessions',
                        lambda a, b: {dt.date(2026, 9, 4), dt.date(2026, 9, 8)})
    tc.clear_cache()
    assert tc.is_session('2026-09-07') is False
    assert tc.next_session('2026-09-04') == dt.date(2026, 9, 8)


def test_out_of_master_range_falls_back(master, monkeypatch):
    monkeypatch.setattr(tc, '_alpaca_sessions', lambda a, b: None)
    # 2030 is beyond the fixture: weekday arithmetic with a warning, never an exception.
    assert tc.next_session('2030-01-04') == dt.date(2030, 1, 7)
