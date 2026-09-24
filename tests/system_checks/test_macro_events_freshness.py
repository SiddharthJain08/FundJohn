"""C3: macro_events_fresh asserts FORWARD coverage, which is the failure mode a
write-recency check cannot see."""
from __future__ import annotations

import datetime as dt

import pandas as pd

from lib import macro_events as me
from src.system_checks.checks import macro_events_freshness as chk
from src.system_checks.checks import master_freshness as mf
from src.system_checks.types import Status

# now-relative, not a fixed calendar date: item 2 (fix round 1) makes
# ingested_at load-bearing for the PASS/WARN tests below, so a hardcoded past
# timestamp would eventually cross MAX_INGEST_AGE_DAYS and flip those tests
# from PASS to WARN as real time passes.
TS = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=1)


def _master(tmp_path, monkeypatch, rows):
    df = pd.DataFrame(rows, columns=me.COLUMNS)
    p = tmp_path / 'macro_events.parquet'
    df.to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    return p


def _row(event, day_offset, ingested_at=TS):
    d = dt.date.today() + dt.timedelta(days=day_offset)
    return {'event': event,
            'scheduled_at': pd.Timestamp(dt.datetime(d.year, d.month, d.day, 12), tz='UTC'),
            'session_date': d, 'source': 'test', 'ingested_at': ingested_at}


def test_skip_when_master_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(tmp_path / 'nope.parquet'))
    status, detail = chk._macro_events_fresh()
    assert status is Status.SKIP and 'absent' in detail


def test_fail_when_master_is_unreadable(tmp_path, monkeypatch):
    p = tmp_path / 'macro_events.parquet'
    p.write_bytes(b'not a parquet file, just garbage bytes 1234567890')
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    status, detail = chk._macro_events_fresh()
    assert status is Status.FAIL and 'unreadable' in detail


def test_fail_when_master_has_no_high_importance_rows(tmp_path, monkeypatch):
    _master(tmp_path, monkeypatch, [_row('PCE', 90)])
    status, detail = chk._macro_events_fresh()
    assert status is Status.FAIL and 'no high-importance' in detail


def test_fail_when_forward_coverage_is_short(tmp_path, monkeypatch):
    _master(tmp_path, monkeypatch, [_row('CPI', 5), _row('NFP', 3),
                                    _row('FOMC_DECISION', -30)])
    status, detail = chk._macro_events_fresh()
    assert status is Status.FAIL and 'forward coverage' in detail


def test_warn_when_one_event_type_has_no_future_rows(tmp_path, monkeypatch):
    # Stale ingested_at on every row: proves the missing-event-type WARN
    # takes precedence over the stale-ingest WARN, not just that it *can*
    # fire in isolation.
    stale = TS - pd.Timedelta(days=90)
    _master(tmp_path, monkeypatch, [_row('CPI', 40, stale), _row('NFP', 35, stale)])
    status, detail = chk._macro_events_fresh()
    assert status is Status.WARN and 'FOMC_DECISION' in detail


def test_warn_when_ingested_at_is_stale(tmp_path, monkeypatch):
    stale = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=60, hours=12)
    _master(tmp_path, monkeypatch, [_row('CPI', 40, stale), _row('NFP', 35, stale),
                                    _row('FOMC_DECISION', 60, stale)])
    status, detail = chk._macro_events_fresh()
    assert status is Status.WARN and '60' in detail


def test_pass_with_full_forward_coverage(tmp_path, monkeypatch):
    _master(tmp_path, monkeypatch, [_row('CPI', 40), _row('NFP', 35),
                                    _row('FOMC_DECISION', 60)])
    status, detail = chk._macro_events_fresh()
    assert status is Status.PASS and 'ahead' in detail
    assert len(detail) < 200


def test_ingested_at_absence_not_penalised(tmp_path, monkeypatch):
    cols = [c for c in me.COLUMNS if c != 'ingested_at']
    rows = [_row('CPI', 40), _row('NFP', 35), _row('FOMC_DECISION', 60)]
    df = pd.DataFrame([{k: v for k, v in r.items() if k != 'ingested_at'}
                        for r in rows], columns=cols)
    p = tmp_path / 'macro_events.parquet'
    df.to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    status, detail = chk._macro_events_fresh()
    assert status is Status.PASS and 'ahead' in detail
    assert len(detail) < 200


def test_check_is_registered_with_the_storage_tag():
    # NOTE (brief deviation): all_checks() returns dict[str, dict] keyed by
    # check name (src/system_checks/registry.py), not a list of objects with
    # .name/.tags attributes as the brief's draft assumed. Matches the access
    # pattern used by tests/system_checks/test_index_integrity_check.py and
    # test_papermint_coverage_check.py.
    from src.system_checks.registry import all_checks
    checks = all_checks()
    assert 'macro_events_fresh' in checks
    assert 'storage' in checks['macro_events_fresh']['tags']


def test_master_freshness_does_not_double_report_macro_events():
    assert 'macro_events.parquet' in mf._COVERED_ELSEWHERE
    assert 'macro_events.parquet' not in mf._CADENCES
