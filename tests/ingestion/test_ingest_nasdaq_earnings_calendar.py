"""Nasdaq keyless earnings calendar → data/master/earnings_calendar_nasdaq.parquet.

`GET https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD` with browser
headers. Unofficial/brittle: 403/429/empty JSON = "day unavailable" (counted,
non-fatal); rc=1 only when ALL days fail. Same-day cross-check of report
TIMING + consensus for the pre-earnings strategies — NOT merged into
earnings.parquet (FMP-fed, separate work).
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from src.ingestion import ingest_nasdaq_earnings_calendar as mod

BMO = {"lastYearRptDt": "8/26/2025", "lastYearEPS": "$2.33", "time": "time-pre-market", "symbol": "BMO",
       "name": "Bank Of Montreal", "marketCap": "$121,887,293,613", "fiscalQuarterEnding": "Jul/2026",
       "epsForecast": "$2.72", "noOfEsts": "3"}
TS = pd.Timestamp('2026-08-23T11:00:00Z')


def _payload(*rows) -> bytes:
    return json.dumps({"data": {"rows": list(rows)}, "message": None, "status": {"rCode": 200}}).encode()


# ── money / time parsing ──────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    ('$2.72', 2.72),
    ('$(0.12)', -0.12),
    ('($0.12)', -0.12),
    ('-$1.05', -1.05),
    ('$121,887,293,613', 121887293613.0),
    ('N/A', None),
    ('', None),
    (None, None),
    ('  ', None),
])
def test_parse_money(raw, expected):
    got = mod.parse_money(raw)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


@pytest.mark.parametrize('raw, expected', [
    ('time-pre-market', 'pre'),
    ('time-after-hours', 'after'),
    ('time-not-supplied', 'unknown'),
    ('', 'unknown'),
    (None, 'unknown'),
    ('something-new', 'unknown'),
])
def test_report_time_mapping(raw, expected):
    assert mod.map_report_time(raw) == expected


@pytest.mark.parametrize('raw, expected', [('3', 3), ('', None), ('N/A', None), (None, None), ('12', 12)])
def test_parse_int(raw, expected):
    assert mod.parse_int(raw) == expected


# ── payload → rows ────────────────────────────────────────────────────────────

def test_payload_row_mapping_matches_sample():
    df = mod.rows_from_payload(_payload(BMO), report_date=date(2026, 8, 25), fetched_at=TS)
    assert list(df.columns) == mod.COLUMNS
    assert len(df) == 1
    r = df.iloc[0]
    assert r['report_date'] == date(2026, 8, 25)
    assert r['ticker'] == 'BMO'
    assert r['company_name'] == 'Bank Of Montreal'
    assert r['report_time'] == 'pre'
    assert r['eps_forecast'] == pytest.approx(2.72)
    assert r['num_estimates'] == 3
    assert r['last_year_eps'] == pytest.approx(2.33)
    assert r['last_year_report_date'] == date(2025, 8, 26)
    assert r['market_cap'] == pytest.approx(121887293613.0)
    assert r['fiscal_quarter_ending'] == 'Jul/2026'
    assert r['source'] == 'nasdaq'
    assert r['fetched_at'] == TS


def test_payload_row_mapping_blanks_and_negatives():
    row = dict(BMO, symbol=' xyz ', epsForecast='$(0.12)', noOfEsts='', lastYearEPS='N/A',
               lastYearRptDt='', marketCap='', time='time-after-hours')
    df = mod.rows_from_payload(_payload(row), report_date=date(2026, 8, 25), fetched_at=TS)
    r = df.iloc[0]
    assert r['ticker'] == 'XYZ'
    assert r['eps_forecast'] == pytest.approx(-0.12)
    assert pd.isna(r['num_estimates']) and pd.isna(r['last_year_eps']) and pd.isna(r['market_cap'])
    assert pd.isna(r['last_year_report_date'])
    assert r['report_time'] == 'after'


def test_payload_without_symbol_rows_dropped_and_empty_rows_is_empty_frame():
    df = mod.rows_from_payload(_payload(dict(BMO, symbol='')), report_date=date(2026, 8, 25), fetched_at=TS)
    assert df.empty and list(df.columns) == mod.COLUMNS
    df = mod.rows_from_payload(_payload(), report_date=date(2026, 8, 25), fetched_at=TS)
    assert df.empty


def test_payload_rows_null_is_not_a_crash():
    body = json.dumps({"data": {"rows": None}}).encode()
    df = mod.rows_from_payload(body, report_date=date(2026, 8, 25), fetched_at=TS)
    assert df.empty


# ── business-day range ────────────────────────────────────────────────────────

def test_business_day_range_back_and_ahead_skips_weekends():
    # today = Sun 2026-08-23; back 3 bdays → Wed 08-19..Fri 08-21; ahead 2 bdays → Mon 08-24, Tue 08-25
    days = mod.business_days(date(2026, 8, 23), days_back=3, days_ahead=2)
    assert days == [date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21),
                    date(2026, 8, 24), date(2026, 8, 25)]


def test_business_day_range_includes_today_when_business_day():
    days = mod.business_days(date(2026, 8, 25), days_back=1, days_ahead=1)
    assert days == [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)]


# ── run(): failure accounting ─────────────────────────────────────────────────

def _patch_http(monkeypatch, responses: dict, calls: list):
    """responses: {'YYYY-MM-DD': (status, body)}; missing → (403, b'')."""
    def fake_get(url, headers, timeout=30):
        d = url.rsplit('date=', 1)[1]
        calls.append((d, headers['User-Agent']))
        return responses.get(d, (403, b''))
    monkeypatch.setattr(mod, '_http_get', fake_get)
    monkeypatch.setattr(mod.time, 'sleep', lambda s: None)


def test_run_counts_days_and_upserts_rows(tmp_path, monkeypatch):
    calls = []
    _patch_http(monkeypatch, {
        '2026-08-24': (200, _payload(dict(BMO, symbol='AAA'))),
        '2026-08-25': (200, _payload(BMO, dict(BMO, symbol='BBB'))),
        '2026-08-26': (429, b''),
        '2026-08-27': (200, b'{"data": null}'),     # empty JSON → unavailable
    }, calls)
    master = tmp_path / 'ec.parquet'
    rc, stats = mod.run(date(2026, 8, 24), days_back=0, days_ahead=3, master_path=master)
    assert [c[0] for c in calls] == ['2026-08-24', '2026-08-25', '2026-08-26', '2026-08-27']
    assert rc == 0
    assert stats['days_ok'] == 2 and stats['days_failed'] == 2
    assert stats['rows'] == 3 and stats['new_rows'] == 3
    out = pd.read_parquet(master)
    assert sorted(out['ticker']) == ['AAA', 'BBB', 'BMO']

    # re-run: same rows → replace, 0 new; a changed timing for BMO overwrites
    calls.clear()
    _patch_http(monkeypatch, {'2026-08-25': (200, _payload(dict(BMO, time='time-after-hours')))}, calls)
    rc, stats = mod.run(date(2026, 8, 25), days_back=0, days_ahead=0, master_path=master)
    assert rc == 0 and stats['days_ok'] == 1 and stats['rows'] == 1 and stats['new_rows'] == 0
    out = pd.read_parquet(master)
    assert len(out) == 3
    assert out.loc[out['ticker'] == 'BMO', 'report_time'].iloc[0] == 'after'
    assert not out.duplicated(subset=['report_date', 'ticker']).any()


def test_run_rotates_user_agents(tmp_path, monkeypatch):
    calls = []
    _patch_http(monkeypatch, {}, calls)
    mod.run(date(2026, 8, 24), days_back=0, days_ahead=3, master_path=tmp_path / 'ec.parquet')
    uas = [c[1] for c in calls]
    assert len(set(uas)) > 1 and all(ua in mod.USER_AGENTS for ua in uas)


def test_run_rc1_only_when_all_days_fail(tmp_path, monkeypatch):
    calls = []
    _patch_http(monkeypatch, {}, calls)    # every day 403
    rc, stats = mod.run(date(2026, 8, 24), days_back=0, days_ahead=2, master_path=tmp_path / 'ec.parquet')
    assert rc == 1 and stats['days_failed'] == 3 and stats['days_ok'] == 0


def test_run_request_exception_is_a_failed_day_not_a_crash(tmp_path, monkeypatch):
    n = {'i': 0}
    def flaky(url, headers, timeout=30):
        n['i'] += 1
        if n['i'] == 1:
            raise OSError('reset')
        return 200, _payload(BMO)
    monkeypatch.setattr(mod, '_http_get', flaky)
    monkeypatch.setattr(mod.time, 'sleep', lambda s: None)
    rc, stats = mod.run(date(2026, 8, 24), days_back=0, days_ahead=1, master_path=tmp_path / 'ec.parquet')
    assert rc == 0 and stats['days_failed'] == 1 and stats['days_ok'] == 1


def test_run_empty_day_with_200_is_ok_not_failed(tmp_path, monkeypatch):
    # a real trading day with no reporters returns rows: [] → day_ok, 0 rows
    calls = []
    _patch_http(monkeypatch, {'2026-08-24': (200, _payload())}, calls)
    rc, stats = mod.run(date(2026, 8, 24), days_back=0, days_ahead=0, master_path=tmp_path / 'ec.parquet')
    assert rc == 0 and stats['days_ok'] == 1 and stats['rows'] == 0


def test_all_null_columns_keep_declared_arrow_types(tmp_path):
    import pyarrow.parquet as pq
    row = dict(BMO, lastYearRptDt='', noOfEsts='', epsForecast='N/A', name='', fiscalQuarterEnding='')
    df = mod.rows_from_payload(_payload(row), report_date=date(2026, 8, 25), fetched_at=TS)
    mod.merge_into_master(df, master_path=tmp_path / 'ec.parquet')
    schema = pq.read_schema(tmp_path / 'ec.parquet')
    assert str(schema.field('report_date').type) == 'date32[day]'
    assert str(schema.field('last_year_report_date').type) == 'date32[day]'
    assert str(schema.field('num_estimates').type) == 'int64'
    assert 'string' in str(schema.field('company_name').type)
    assert 'string' in str(schema.field('fiscal_quarter_ending').type)
    assert str(schema.field('eps_forecast').type) == 'double'
