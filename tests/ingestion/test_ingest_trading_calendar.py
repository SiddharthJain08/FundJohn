from __future__ import annotations
import datetime as dt
import json
import pandas as pd

from ingestion import ingest_trading_calendar as itc


def _fake_cli(payload_by_year):
    def run_cli(start: str, end: str) -> str:
        return json.dumps(payload_by_year[int(start[:4])])
    return run_cli


def test_fetch_year_parses_sessions():
    rows = itc.fetch_year(2026, _fake_cli({2026: [
        {'date': '2026-09-04', 'open': '09:30', 'close': '16:00',
         'session_open': '0400', 'session_close': '2000', 'settlement_date': '2026-09-08'}]}))
    assert rows == [{'date': dt.date(2026, 9, 4), 'open': '09:30', 'close': '16:00',
                     'session_open': '0400', 'session_close': '2000',
                     'settlement_date': dt.date(2026, 9, 8)}]


def test_build_rows_stamps_active_source_fetched_at():
    df = itc.build_rows([2026], _fake_cli({2026: [
        {'date': '2026-09-04', 'open': '09:30', 'close': '16:00'}]}))
    assert list(df.columns) == itc.COLUMNS
    assert bool(df.loc[0, 'active']) is True and df.loc[0, 'source'] == 'alpaca'
    assert df.loc[0, 'fetched_at'].endswith('Z')


def test_mark_removed_flags_sessions_the_exchange_dropped():
    existing = pd.DataFrame({'date': [dt.date(2026, 9, 4), dt.date(2026, 9, 7), dt.date(2027, 1, 4)],
                             'open': ['09:30'] * 3, 'close': ['16:00'] * 3,
                             'session_open': ['0400'] * 3, 'session_close': ['2000'] * 3,
                             'settlement_date': [None] * 3, 'active': [True] * 3,
                             'source': ['alpaca'] * 3, 'fetched_at': ['x'] * 3})
    fetched = itc.build_rows([2026], _fake_cli({2026: [{'date': '2026-09-04', 'open': '09:30', 'close': '16:00'}]}))
    removed = itc.mark_removed(existing, fetched, [2026])
    assert removed['date'].tolist() == [dt.date(2026, 9, 7)]      # 2027 untouched: not a fetched year
    assert removed['active'].tolist() == [False]


def test_main_writes_master_and_is_idempotent(tmp_path, monkeypatch):
    payload = {2026: [{'date': '2026-09-04', 'open': '09:30', 'close': '16:00'},
                      {'date': '2026-09-08', 'open': '09:30', 'close': '16:00'}]}
    monkeypatch.setattr(itc, '_run_cli', _fake_cli(payload))
    out = tmp_path / 'trading_calendar.parquet'
    assert itc.main(['--start-year', '2026', '--end-year', '2026', '--path', str(out)]) == 0
    assert itc.main(['--start-year', '2026', '--end-year', '2026', '--path', str(out)]) == 0
    df = pd.read_parquet(out)
    assert sorted(df['date'].astype(str)) == ['2026-09-04', '2026-09-08']
    assert df['active'].all()


def test_main_skips_deactivation_for_a_year_that_returned_no_sessions(tmp_path, monkeypatch, caplog):
    import logging
    full_2026 = [{'date': d.strftime('%Y-%m-%d'), 'open': '09:30', 'close': '16:00'}
                 for d in pd.bdate_range('2026-01-02', '2026-12-31')]
    out = tmp_path / 'trading_calendar.parquet'
    monkeypatch.setattr(itc, '_run_cli', _fake_cli({2026: full_2026}))
    assert itc.main(['--start-year', '2026', '--end-year', '2026', '--path', str(out)]) == 0
    monkeypatch.setattr(itc, '_run_cli', _fake_cli({2026: []}))
    with caplog.at_level(logging.WARNING):
        rc = itc.main(['--start-year', '2026', '--end-year', '2026', '--path', str(out)])
    assert rc == 1                                   # nothing fetched at all → master untouched
    df = pd.read_parquet(out)
    assert df['active'].all() and len(df) == len(full_2026)
    # one healthy year + one empty year: the empty year is NOT deactivated
    full_2027 = [{'date': d.strftime('%Y-%m-%d'), 'open': '09:30', 'close': '16:00'}
                 for d in pd.bdate_range('2027-01-04', '2027-12-31')]
    monkeypatch.setattr(itc, '_run_cli', _fake_cli({2026: [], 2027: full_2027}))
    with caplog.at_level(logging.WARNING):
        assert itc.main(['--start-year', '2026', '--end-year', '2027', '--path', str(out)]) == 0
    df = pd.read_parquet(out)
    assert df[pd.to_datetime(df['date']).dt.year == 2026]['active'].all()
    assert (pd.to_datetime(df['date']).dt.year == 2027).sum() == len(full_2027)
    assert any('NO deactivation' in r.message for r in caplog.records)
