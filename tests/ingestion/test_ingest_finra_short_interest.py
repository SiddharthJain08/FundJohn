"""FINRA biweekly consolidated short interest → data/master/short_interest.parquet.

`GET https://cdn.finra.org/equity/otcmarket/biweekly/shrt{YYYYMMDD}.csv` is a
keyless pipe-delimited file published ~9 business days after each settlement
date (the 15th and the last business day of the month, rolled BACK over
weekends/holidays). 403 = not a settlement date OR not yet published — INFO,
never an error. The CSV's own `settlementDate` column is authoritative.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.ingestion import ingest_finra_short_interest as mod

HEADER = ('accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|'
          'marketClassCode|currentShortPositionQuantity|previousShortPositionQuantity|'
          'stockSplitFlag|averageDailyVolumeQuantity|daysToCoverQuantity|revisionFlag|'
          'changePercent|changePreviousNumber|settlementDate')
SAMPLE_ROW = '20260731|A|Agilent Technologies Inc.|A|NYSE|5749623|7538437||2301495|2.50||-23.73|-1788814|2026-07-31'


def _csv(*rows: str) -> bytes:
    return ('\n'.join([HEADER, *rows]) + '\n').encode()


# ── candidate settlement dates ────────────────────────────────────────────────

def test_candidates_15th_on_saturday_rolls_back_to_friday_14th():
    # 2026-08-15 is a Saturday → Fri 2026-08-14
    cands = list(mod.candidate_settlement_dates(date(2026, 8, 1), date(2026, 8, 20)))
    assert cands == [date(2026, 8, 14)]


def test_candidates_month_end_on_sunday_rolls_back_to_friday():
    # 2026-05-31 is a Sunday → Fri 2026-05-29; 2026-05-15 is a Friday (unchanged)
    cands = list(mod.candidate_settlement_dates(date(2026, 5, 1), date(2026, 5, 31)))
    assert cands == [date(2026, 5, 15), date(2026, 5, 29)]


def test_candidates_span_months_in_order_and_respect_window_bounds():
    cands = list(mod.candidate_settlement_dates(date(2026, 6, 16), date(2026, 7, 31)))
    # 2026-06-30 Tue, 2026-07-15 Wed, 2026-07-31 Fri
    assert cands == [date(2026, 6, 30), date(2026, 7, 15), date(2026, 7, 31)]


# ── CSV → rows ────────────────────────────────────────────────────────────────

def test_csv_row_mapping_matches_sample():
    df = mod.rows_from_csv(_csv(SAMPLE_ROW), fetched_at=pd.Timestamp('2026-08-23T12:00:00Z'))
    assert len(df) == 1
    r = df.iloc[0]
    assert r['settlement_date'] == date(2026, 7, 31)
    assert r['ticker'] == 'A'
    assert r['issue_name'] == 'Agilent Technologies Inc.'
    assert r['exchange'] == 'A'
    assert r['market_class'] == 'NYSE'
    assert r['short_interest'] == 5749623 and str(df['short_interest'].dtype).lower().startswith('int')
    assert r['prev_short_interest'] == 7538437
    assert r['avg_daily_volume'] == 2301495
    assert r['days_to_cover'] == pytest.approx(2.50)
    assert r['change_pct'] == pytest.approx(-23.73)
    assert r['change_shares'] == -1788814
    assert pd.isna(r['split_flag']) and pd.isna(r['revision_flag'])
    assert r['source'] == 'finra'
    assert r['fetched_at'] == pd.Timestamp('2026-08-23T12:00:00Z')
    assert list(df.columns) == mod.COLUMNS


def test_csv_row_mapping_normalises_ticker_and_keeps_blank_numerics_null():
    row = '20260731| brk/b |Berkshire B|B|NYSE|100||Y||||||2026-07-31'
    df = mod.rows_from_csv(_csv(row), fetched_at=pd.Timestamp('2026-08-23T12:00:00Z'))
    r = df.iloc[0]
    assert r['ticker'] == 'BRK/B'
    assert pd.isna(r['prev_short_interest']) and pd.isna(r['days_to_cover'])
    assert r['split_flag'] == 'Y'


def test_csv_row_mapping_drops_rows_without_ticker():
    df = mod.rows_from_csv(_csv(SAMPLE_ROW, '20260731||No Symbol|A|NYSE|1|1||1|1.0|||0|2026-07-31'),
                           fetched_at=pd.Timestamp('2026-08-23T12:00:00Z'))
    assert list(df['ticker']) == ['A']


# ── master merge: replace on revision ─────────────────────────────────────────

def test_merge_replaces_revised_row_and_keeps_other_dates_and_tickers(tmp_path):
    master = tmp_path / 'short_interest.parquet'
    ts = pd.Timestamp('2026-08-23T12:00:00Z')
    first = mod.rows_from_csv(_csv(SAMPLE_ROW, '20260731|MSFT|Microsoft|M|NASDAQ|10|9||5|2.0|||1|2026-07-31'), fetched_at=ts)
    stats = mod.merge_into_master(first, master_path=master)
    assert stats == {'fetched': 2, 'new_rows': 2, 'replaced_rows': 0, 'master_rows_after': 2}

    # FINRA revises A for 07-31 (revisionFlag=R), plus a new settlement date for MSFT
    revised = mod.rows_from_csv(_csv(
        '20260731|A|Agilent Technologies Inc.|A|NYSE|6000000|7538437||2301495|2.61|R|-20.41|-1538437|2026-07-31',
        '20260815|MSFT|Microsoft|M|NASDAQ|11|10||5|2.2|||1|2026-08-14',
    ), fetched_at=ts + pd.Timedelta(days=1))
    stats = mod.merge_into_master(revised, master_path=master)
    assert stats == {'fetched': 2, 'new_rows': 1, 'replaced_rows': 1, 'master_rows_after': 3}

    out = pd.read_parquet(master)
    assert len(out) == 3
    a = out[(out['ticker'] == 'A')].iloc[0]
    assert a['short_interest'] == 6000000 and a['revision_flag'] == 'R'
    assert sorted(out['settlement_date'].astype(str).unique()) == ['2026-07-31', '2026-08-14']
    assert not out.duplicated(subset=['settlement_date', 'ticker']).any()


def test_present_settlement_dates_reads_only_the_key_column(tmp_path):
    master = tmp_path / 'short_interest.parquet'
    assert mod.present_settlement_dates(master) == set()
    df = mod.rows_from_csv(_csv(SAMPLE_ROW), fetched_at=pd.Timestamp('2026-08-23T12:00:00Z'))
    mod.merge_into_master(df, master_path=master)
    assert mod.present_settlement_dates(master) == {date(2026, 7, 31)}


# ── run(): 403 handling, skip-present, rc semantics ───────────────────────────

def _patch_http(monkeypatch, responses: dict, calls: list):
    """responses: {'YYYYMMDD': (status, body)} — anything missing → 403."""
    def fake_get(url, timeout=30):
        ymd = url.rsplit('shrt', 1)[1].split('.')[0]
        calls.append(ymd)
        return responses.get(ymd, (403, b''))
    monkeypatch.setattr(mod, '_http_get', fake_get)
    monkeypatch.setattr(mod.time, 'sleep', lambda s: None)


def test_run_403_is_unpublished_not_error_and_tries_two_earlier_bdays(tmp_path, monkeypatch):
    calls = []
    _patch_http(monkeypatch, {}, calls)
    master = tmp_path / 'short_interest.parquet'
    rc, stats = mod.run(date(2026, 8, 1), date(2026, 8, 20), master_path=master)
    # candidate Fri 08-14 → 403 → Thu 08-13 → 403 → Wed 08-12 → 403 : unpublished
    assert calls == ['20260814', '20260813', '20260812']
    assert rc == 0
    assert stats['files_unpublished'] == 1 and stats['files_fetched'] == 0 and stats['files_error'] == 0
    assert not master.exists()


def test_run_holiday_candidate_falls_back_and_stores_csv_settlement_date(tmp_path, monkeypatch):
    calls = []
    # Pretend Fri 2026-05-15 is a holiday: the file lives under the 14th and says so.
    body = _csv('20260515|A|Agilent|A|NYSE|1|1||1|1.0|||0|2026-05-14')
    _patch_http(monkeypatch, {'20260514': (200, body)}, calls)
    master = tmp_path / 'short_interest.parquet'
    rc, stats = mod.run(date(2026, 5, 1), date(2026, 5, 20), master_path=master)
    assert calls == ['20260515', '20260514']
    assert rc == 0 and stats['files_fetched'] == 1 and stats['new_rows'] == 1
    assert mod.present_settlement_dates(master) == {date(2026, 5, 14)}


def test_run_skips_settlement_dates_already_present_unless_force(tmp_path, monkeypatch):
    calls = []
    body = _csv(SAMPLE_ROW)
    _patch_http(monkeypatch, {'20260731': (200, body)}, calls)
    master = tmp_path / 'short_interest.parquet'
    rc, stats = mod.run(date(2026, 7, 20), date(2026, 7, 31), master_path=master)
    assert rc == 0 and stats['files_fetched'] == 1 and stats['new_rows'] == 1
    assert calls == ['20260731']

    calls.clear()
    rc, stats = mod.run(date(2026, 7, 20), date(2026, 7, 31), master_path=master)
    assert rc == 0 and stats['files_skipped'] == 1 and stats['files_fetched'] == 0
    assert calls == []

    calls.clear()
    rc, stats = mod.run(date(2026, 7, 20), date(2026, 7, 31), master_path=master, force=True)
    assert rc == 0 and stats['files_fetched'] == 1 and stats['replaced_rows'] == 1 and stats['new_rows'] == 0
    assert calls == ['20260731']


def test_run_skip_present_covers_holiday_rolled_dates(tmp_path, monkeypatch):
    calls = []
    body = _csv('20260515|A|Agilent|A|NYSE|1|1||1|1.0|||0|2026-05-14')
    _patch_http(monkeypatch, {'20260514': (200, body)}, calls)
    master = tmp_path / 'short_interest.parquet'
    mod.run(date(2026, 5, 1), date(2026, 5, 20), master_path=master)
    calls.clear()
    rc, stats = mod.run(date(2026, 5, 1), date(2026, 5, 20), master_path=master)
    assert calls == [] and stats['files_skipped'] == 1


def test_run_non_403_http_errors_are_counted_rc0_when_any_candidate_resolves(tmp_path, monkeypatch):
    calls = []
    _patch_http(monkeypatch, {'20260715': (500, b''), '20260714': (500, b''), '20260713': (500, b''),
                              '20260731': (200, _csv(SAMPLE_ROW))}, calls)
    master = tmp_path / 'short_interest.parquet'
    rc, stats = mod.run(date(2026, 7, 1), date(2026, 7, 31), master_path=master)
    assert rc == 0
    assert stats['files_error'] == 1 and stats['files_fetched'] == 1


def test_run_rc1_only_when_every_candidate_fails_with_non_403(tmp_path, monkeypatch):
    calls = []
    _patch_http(monkeypatch, {'20260715': (500, b''), '20260714': (500, b''), '20260713': (500, b''),
                              '20260731': (502, b''), '20260730': (502, b''), '20260729': (502, b'')}, calls)
    master = tmp_path / 'short_interest.parquet'
    rc, stats = mod.run(date(2026, 7, 1), date(2026, 7, 31), master_path=master)
    assert rc == 1 and stats['files_error'] == 2


def test_run_rc1_when_first_request_raises(tmp_path, monkeypatch):
    def boom(url, timeout=30):
        raise OSError('dns down')
    monkeypatch.setattr(mod, '_http_get', boom)
    monkeypatch.setattr(mod.time, 'sleep', lambda s: None)
    master = tmp_path / 'short_interest.parquet'
    rc, stats = mod.run(date(2026, 7, 1), date(2026, 7, 31), master_path=master)
    assert rc == 1 and stats['files_error'] >= 1


def test_run_mixed_unpublished_and_error_is_rc0(tmp_path, monkeypatch):
    # 07-15 errors, 07-31 unpublished (403): the provider WAS readable → rc=0
    calls = []
    _patch_http(monkeypatch, {'20260715': (500, b''), '20260714': (500, b''), '20260713': (500, b'')}, calls)
    master = tmp_path / 'short_interest.parquet'
    rc, stats = mod.run(date(2026, 7, 1), date(2026, 7, 31), master_path=master)
    assert rc == 0 and stats['files_error'] == 1 and stats['files_unpublished'] == 1


def test_all_null_columns_keep_declared_arrow_types(tmp_path):
    import pyarrow.parquet as pq
    df = mod.rows_from_csv(_csv(SAMPLE_ROW), fetched_at=pd.Timestamp('2026-08-23T12:00:00Z'))
    mod.merge_into_master(df, master_path=tmp_path / 'si.parquet')
    schema = pq.read_schema(tmp_path / 'si.parquet')
    assert str(schema.field('settlement_date').type) == 'date32[day]'
    assert 'string' in str(schema.field('split_flag').type)
    assert 'string' in str(schema.field('revision_flag').type)
    assert str(schema.field('short_interest').type) == 'int64'
    assert str(schema.field('days_to_cover').type) == 'double'
    assert str(schema.field('fetched_at').type).startswith('timestamp[')
