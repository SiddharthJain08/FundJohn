"""C3 ingester: Fed / BLS / BEA keyless parsers over the checked-in fixtures,
session_date derivation, and the append_dedup merge.

NO NETWORK. Every test parses a fixture from disk; the HTTP layer is only
exercised through injected `from_file` paths.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from lib import trading_calendar as tc
from src.ingestion import ingest_macro_events as mod

FIX = Path(__file__).resolve().parents[1] / 'fixtures' / 'macro_events'
TS = pd.Timestamp('2026-09-13T12:00:00Z')


@pytest.fixture(autouse=True)
def calendar(tmp_path, monkeypatch):
    """Sessions Mon-Fri 2026-01-01..2027-12-31, minus 2026-09-07 (Labor Day)."""
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-01-01', '2027-12-31')
            if d.date() != dt.date(2026, 9, 7)]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions',
                        lambda a, b: (_ for _ in ()).throw(AssertionError('no alpaca probe')))
    yield
    tc.clear_cache()


def _by_event(df):
    return {e: sorted(g['session_date']) for e, g in df.groupby('event')}


# ── Fed ─────────────────────────────────────────────────────────────────────

def test_fed_parses_decision_dates_as_the_last_day_of_each_range():
    df = mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(), fetched_at=TS)
    got = _by_event(df)
    assert got['FOMC_DECISION'] == [dt.date(2026, 1, 28), dt.date(2026, 4, 29),
                                    dt.date(2026, 9, 17), dt.date(2026, 12, 16),
                                    dt.date(2027, 1, 27)]


def test_fed_minutes_release_dates_are_not_mistaken_for_meetings():
    df = mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(), fetched_at=TS)
    got = _by_event(df)
    assert got['FOMC_MINUTES'] == [dt.date(2026, 2, 18), dt.date(2026, 5, 20)]
    assert dt.date(2026, 2, 18) not in got['FOMC_DECISION']
    assert dt.date(2026, 5, 20) not in got['FOMC_DECISION']


def test_fed_decision_is_stamped_1400_et():
    df = mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(), fetched_at=TS)
    row = df[df['session_date'] == dt.date(2026, 9, 17)].iloc[0]
    assert row['scheduled_at'] == pd.Timestamp('2026-09-17T18:00:00Z')   # 14:00 EDT
    assert row['source'] == 'federalreserve.gov'


def test_fed_empty_html_is_an_empty_frame_not_a_crash():
    df = mod.parse_fed('<html><body>nothing here</body></html>', fetched_at=TS)
    assert df.empty and list(df.columns) == mod.COLUMNS


# ── BLS ─────────────────────────────────────────────────────────────────────

def test_bls_single_release_page_uses_the_default_event():
    df = mod.parse_titled((FIX / 'bls_cpi_sched.html').read_text(), (),
                          'bls.gov', fetched_at=TS, default_event='CPI')
    assert _by_event(df)['CPI'] == [dt.date(2026, 9, 11), dt.date(2026, 10, 13)]
    assert df.iloc[0]['scheduled_at'] == pd.Timestamp('2026-09-11T12:30:00Z')  # 08:30 EDT


def test_bls_annual_page_maps_titles_to_events_and_drops_the_rest():
    df = mod.parse_titled((FIX / 'bls_annual_sched.html').read_text(),
                          mod.BLS_TITLES, 'bls.gov', fetched_at=TS)
    got = _by_event(df)
    assert got == {'CPI': [dt.date(2026, 9, 11)], 'NFP': [dt.date(2026, 9, 4)]}


# ── BEA ─────────────────────────────────────────────────────────────────────

def test_bea_advance_gdp_and_pce_only():
    df = mod.parse_titled((FIX / 'bea_schedule.html').read_text(), mod.BEA_TITLES,
                          'bea.gov', fetched_at=TS)
    got = _by_event(df)
    assert got == {'GDP_ADV': [dt.date(2026, 7, 30)], 'PCE': [dt.date(2026, 8, 28)]}


def test_bea_second_estimate_does_not_leak_advance_from_the_previous_record():
    """The per-record context window is bounded by the PREVIOUS datetime match,
    so 'Advance Estimate' one item earlier cannot re-tag the second estimate."""
    df = mod.parse_titled((FIX / 'bea_schedule.html').read_text(), mod.BEA_TITLES,
                          'bea.gov', fetched_at=TS)
    assert dt.date(2026, 8, 27) not in list(df['session_date'])


# ── session_date ────────────────────────────────────────────────────────────

def test_session_date_is_the_same_day_for_a_pre_open_release():
    utc = pd.Timestamp('2026-09-11T12:30:00Z').to_pydatetime()
    assert mod.session_date_for(utc) == dt.date(2026, 9, 11)


def test_session_date_rolls_a_holiday_release_forward():
    utc = pd.Timestamp('2026-09-07T12:30:00Z').to_pydatetime()   # Labor Day
    assert mod.session_date_for(utc) == dt.date(2026, 9, 8)


# ── master merge ────────────────────────────────────────────────────────────

def test_merge_dedups_on_event_and_scheduled_at(tmp_path):
    master = tmp_path / 'macro_events.parquet'
    df = mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(), fetched_at=TS)
    first = mod.merge_into_master(df, master_path=master)
    second = mod.merge_into_master(df, master_path=master)
    assert first['new_rows'] == len(df)
    assert second['new_rows'] == 0
    assert second['master_rows_after'] == first['master_rows_after']


def test_merge_is_additive_across_sources(tmp_path):
    master = tmp_path / 'macro_events.parquet'
    mod.merge_into_master(mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(),
                                        fetched_at=TS), master_path=master)
    mod.merge_into_master(mod.parse_titled((FIX / 'bls_cpi_sched.html').read_text(), (),
                                           'bls.gov', fetched_at=TS,
                                           default_event='CPI'),
                          master_path=master)
    out = pd.read_parquet(master)
    assert set(out['event']) >= {'FOMC_DECISION', 'FOMC_MINUTES', 'CPI'}
    assert list(out.columns) == mod.COLUMNS


# ── the reader sees what the ingester wrote ─────────────────────────────────

def test_reader_round_trip(tmp_path, monkeypatch):
    from lib import macro_events as me
    master = tmp_path / 'macro_events.parquet'
    mod.merge_into_master(mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(),
                                        fetched_at=TS), master_path=master)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(master))
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert got == {dt.date(2026, 9, 16): ['FOMC_DECISION'],
                   dt.date(2026, 9, 17): ['FOMC_DECISION']}


# ── run() wiring, via --from-file only (no network) ─────────────────────────

def test_run_from_file_writes_the_master_and_counts(tmp_path):
    master = tmp_path / 'macro_events.parquet'
    rc, stats = mod.run(['fed'], master_path=master,
                        from_file={'fed': str(FIX / 'fed_fomccalendars.html')})
    assert rc == 0
    assert stats['urls_ok'] == 1 and stats['urls_failed'] == 0
    assert stats['new_rows'] == 7          # 5 decisions + 2 minutes
    assert master.exists()


def test_run_reports_a_failure_without_raising(tmp_path):
    master = tmp_path / 'macro_events.parquet'
    rc, stats = mod.run(['fed'], master_path=master,
                        from_file={'fed': str(tmp_path / 'missing.html')})
    assert rc == 1 and stats['urls_failed'] == 1 and stats['new_rows'] == 0
