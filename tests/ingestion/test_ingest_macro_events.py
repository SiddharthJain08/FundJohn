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


# ── T8 fix-round-1 item 3a: parser hardening ─────────────────────────────────

def test_fed_furniture_trap_link_text_and_footer_produce_no_spurious_rows():
    """Three meetings: Jan (minutes complete, link text before the release
    date), Apr (minutes label present but PENDING — no release date at all,
    sandwiched between the other two), Sep (minutes complete, link text). A
    trailing 'Last Update: September 12, 2026' footer. Regression for two
    bugs at once: (1) a bare 'Month D' (the footer) must never yield a
    decision, and (2) Apr's dangling 'Minutes:' must not lazily skip across
    Sep's OWN day-range digits to steal ITS '(Released ...)' clause, which
    would both mis-pair the minutes date and erase Sep's decision when the
    over-matched span gets blanked out."""
    df = mod.parse_fed((FIX / 'fed_furniture_trap.html').read_text(), fetched_at=TS)
    got = _by_event(df)
    assert got == {'FOMC_DECISION': [dt.date(2026, 1, 28), dt.date(2026, 4, 29),
                                     dt.date(2026, 9, 17)],
                   'FOMC_MINUTES': [dt.date(2026, 2, 18), dt.date(2026, 10, 21)]}
    assert len(df) == 5


def test_single_month_day_is_not_a_decision():
    html = ('<html><body><h4>2026 FOMC Meeting</h4>'
            '<p>Last Update: September 12, 2026</p></body></html>')
    df = mod.parse_fed(html, fetched_at=TS)
    assert df.empty


# ── T8 fix-round-1 item 4c: _iter_datetimes bounds the FIRST record too ─────

def test_iter_datetimes_bounds_the_first_records_context():
    """Page furniture mentioning 'gross domestic product ... advance' sits
    well before the first real record, separated from it by neutral padding
    longer than _CONTEXT_LOOKBACK_CHARS; the record's own title is 'Personal
    Income and Outlays' (PCE). An unbounded ctx = text[0:m.start()] for the
    first match would let that distant furniture mis-tag it as GDP_ADV; a
    properly bounded ctx only reaches back into the neutral padding."""
    header = ('The Bureau of Economic Analysis publishes a gross domestic '
             'product advance estimate release schedule for public reference.')
    padding = 'Neutral padding text with no macro release keywords here. ' * 6
    assert len(padding) > mod._CONTEXT_LOOKBACK_CHARS   # the trap only bites if this holds
    html = (f'<html><body><p>{header}</p><p>{padding}</p><ul>'
            '<li><span class="title">Personal Income and Outlays, July 2026</span> '
            '<span class="date">August 28, 2026 8:30 a.m. EDT</span></li></ul>'
            '</body></html>')
    df = mod.parse_titled(html, mod.BEA_TITLES, 'bea.gov', fetched_at=TS)
    assert list(df['event']) == ['PCE']


# ── T8 fix-round-1 item 2: calendar-coverage guard ──────────────────────────

def test_calendar_guard_refuses_write_when_master_lacks_margin(tmp_path, monkeypatch):
    """A calendar covering every individual event date (so parsing itself
    succeeds via is_session's single-date check) but NOT the +-14d margin the
    write guard requires. The guard must catch this itself, not rely on
    is_session silently degrading to the (here tripwired) alpaca fallback."""
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-01-28', '2027-01-27')]
    cal_p = tmp_path / 'tight_cal.parquet'
    pd.DataFrame(rows).to_parquet(cal_p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(cal_p))
    tc.clear_cache()

    master = tmp_path / 'macro_events.parquet'
    rc, stats = mod.run(['fed'], master_path=master,
                        from_file={'fed': str(FIX / 'fed_fomccalendars.html')})
    assert rc == 3
    assert not master.exists()


def test_deactivate_refuses_write_when_calendar_lacks_margin(tmp_path, monkeypatch):
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-09-16', '2026-09-17')]
    cal_p = tmp_path / 'tight_cal2.parquet'
    pd.DataFrame(rows).to_parquet(cal_p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(cal_p))
    tc.clear_cache()

    master = tmp_path / 'macro_events.parquet'
    rc, stats = mod.run_deactivate([('FOMC_DECISION', '2026-09-17T18:00:00Z')],
                                   master_path=master)
    assert rc == 3
    assert not master.exists()


# ── T8 fix-round-1 item 1: active=False correctability ──────────────────────

def test_deactivate_round_trip_reader_and_no_resurrection(tmp_path, monkeypatch):
    master = tmp_path / 'macro_events.parquet'
    df = mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(), fetched_at=TS)
    mod.merge_into_master(df, master_path=master)

    row = df[(df['event'] == 'FOMC_DECISION') &
             (df['session_date'] == dt.date(2026, 9, 17))].iloc[0]
    iso = row['scheduled_at'].isoformat()
    rc, stats = mod.run_deactivate([('FOMC_DECISION', iso)], master_path=master)
    assert rc == 0
    assert stats['unmatched'] == []

    from lib import macro_events as me
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(master))
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert dt.date(2026, 9, 16) not in got
    assert dt.date(2026, 9, 17) not in got

    # Re-ingesting the SAME fed page must not resurrect the deactivated row —
    # a normal parse never carries active=True explicitly (see _row()).
    mod.merge_into_master(df, master_path=master)
    got2 = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert dt.date(2026, 9, 16) not in got2
    assert dt.date(2026, 9, 17) not in got2


def test_deactivate_with_mismatched_timestamp_is_loud_not_silent(tmp_path, monkeypatch):
    """An ISO that doesn't match the ingested scheduled_at to the second must
    not report a quiet success — the operator needs to know the real row is
    still active."""
    master = tmp_path / 'macro_events.parquet'
    df = mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(), fetched_at=TS)
    mod.merge_into_master(df, master_path=master)

    rc, stats = mod.run_deactivate([('FOMC_DECISION', '2026-09-17T00:00:00Z')],
                                   master_path=master)
    assert rc == 4
    assert stats['unmatched'] == ['FOMC_DECISION=2026-09-17T00:00:00+00:00']

    from lib import macro_events as me
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(master))
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert got == {dt.date(2026, 9, 16): ['FOMC_DECISION'],
                   dt.date(2026, 9, 17): ['FOMC_DECISION']}


def test_deactivate_refuses_to_combine_with_backfill():
    rc = mod.main(['--deactivate', 'CPI=2026-09-16T12:30:00Z', '--backfill'])
    assert rc == 2


# ── T8 fix-round-1 item 4b: --from-file matching ─────────────────────────────

def test_from_file_matches_exact_key_and_four_digit_year_suffix():
    assert mod._from_file_matches('fed', 'fed')
    assert mod._from_file_matches('fed2020', 'fed')
    assert not mod._from_file_matches('bls_cpi', 'bls')
    assert not mod._from_file_matches('bls_empsit', 'bls')
    assert not mod._from_file_matches('fed20200', 'fed')   # 5 digits, not exactly 4
    assert not mod._from_file_matches('fedabcd', 'fed')    # not digits


def test_unmatched_from_file_key_is_an_error_not_a_silent_live_fetch(tmp_path):
    master = tmp_path / 'macro_events.parquet'
    rc, stats = mod.run(['fed'], master_path=master,
                        from_file={'nonexistent_source': str(FIX / 'fed_fomccalendars.html')})
    assert rc == 2
    assert not master.exists()


# ── T8 fix-round-1 item 4a: --dry-run prints rows ────────────────────────────

def test_dry_run_prints_rows_and_leaves_the_master_absent(tmp_path, capsys):
    master = tmp_path / 'macro_events.parquet'
    rc, stats = mod.run(['fed'], master_path=master, dry_run=True,
                        from_file={'fed': str(FIX / 'fed_fomccalendars.html')})
    assert rc == 0
    assert not master.exists()
    out = capsys.readouterr().out
    assert 'DRY-RUN fed: 7 rows' in out
    assert 'FOMC_DECISION' in out


# ── T8 fix-round-1 item 4d: zero-row parse is a soft failure ────────────────

def test_zero_row_parse_is_a_soft_failure_unless_allow_empty(tmp_path):
    master = tmp_path / 'macro_events.parquet'
    empty_html = tmp_path / 'empty.html'
    empty_html.write_text('<html><body>nothing here</body></html>')

    rc, stats = mod.run(['fed'], master_path=master, from_file={'fed': str(empty_html)})
    assert rc == 1
    assert stats['urls_ok'] == 1
    assert stats['urls_empty'] == 1
    assert stats['urls_failed'] == 0

    rc2, stats2 = mod.run(['fed'], master_path=master, from_file={'fed': str(empty_html)},
                          allow_empty=True)
    assert rc2 == 0
    assert stats2['urls_empty'] == 1


# ── T8 fix-round-1 item 4e: _jobs() bounded by --end-year ───────────────────

def test_jobs_bounded_by_end_year_for_backfill():
    jobs = mod._jobs({'fed'}, backfill=True, start_year=2020, end_year=2022)
    assert [k for k, _, _ in jobs] == ['fed', 'fed2020', 'fed2021', 'fed2022']

    jobs2 = mod._jobs({'bls'}, backfill=True, start_year=2025, end_year=2025)
    assert [k for k, _, _ in jobs2] == ['bls_cpi', 'bls_empsit', 'bls2025']


def test_jobs_full_backfill_matches_the_22_archive_pages_the_report_cites():
    jobs = mod._jobs({'fed', 'bls', 'bea'}, backfill=True, start_year=2017, end_year=2027)
    assert len(jobs) == 26
    archive = [k for k, _, _ in jobs if k not in ('fed', 'bls_cpi', 'bls_empsit', 'bea')]
    assert len(archive) == 22
