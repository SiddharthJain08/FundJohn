"""scripts/apply_qd_manifest_hygiene.py — operator script for Stream A Task 6
manifest hygiene (spec 2026-09-12-quantdinger-adoptions-spec.md §A4).

Exercises the script ONLY against a tmp_path copy of a small fixture
manifest — never the real src/strategies/manifest.json. Covers: dry-run
writes nothing; apply makes exactly the expected changes; a second apply is
a no-op; a strategy in an unexpected pre-state makes the whole locked
read-modify-write refuse (and, because both targets share one lock
acquisition, a bad precondition on one leaves NEITHER target written);
entries outside the two named strategies are never touched.

Run: python3 -m pytest tests/scripts/test_apply_qd_manifest_hygiene.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    'apply_qd_manifest_hygiene', ROOT / 'scripts' / 'apply_qd_manifest_hygiene.py')
hygiene = importlib.util.module_from_spec(_SPEC)
sys.modules['apply_qd_manifest_hygiene'] = hygiene
_SPEC.loader.exec_module(hygiene)

TR04, TR06 = hygiene.TARGETS


@pytest.fixture(autouse=True)
def _no_postgres(monkeypatch):
    # _persist_lifecycle_event (called from LifecycleStateMachine.transition)
    # no-ops without this; never touch real Postgres from a unit test.
    monkeypatch.delenv('POSTGRES_URI', raising=False)


def _target_entry(reason: str = None, state: str = 'archived') -> dict:
    reason = hygiene.OLD_REASON if reason is None else reason
    return {
        'state': state,
        'state_since': '2026-04-30T23:37:01.649Z',
        'metadata': {'canonical_file': 'str04_zarattini_intraday_spy.py',
                     'class': 'ZarattiniIntradaySpy'},
        'history': [
            {'from_state': 'staging', 'to_state': 'candidate',
             'timestamp': '2026-04-01T00:00:00Z', 'actor': 'system',
             'reason': 'Auto-backtest: Sharpe 0.00, DD 0.0%, trades 0', 'metadata': {}},
            {'from_state': 'candidate', 'to_state': state,
             'timestamp': '2026-04-30T23:37:01.649Z', 'actor': 'system',
             'reason': reason, 'metadata': {}},
        ],
        'instrument_class': 'equity',
    }


def _fixture(tmp_path, tr04=None, tr06=None) -> Path:
    data = {
        'schema_version': '1.0',
        'updated_at': '2026-09-12T00:00:00Z',
        'strategies': {
            TR04: tr04 if tr04 is not None else _target_entry(),
            TR06: tr06 if tr06 is not None else _target_entry(),
            # Control entry — must never be touched by the script.
            'S_control_untouched': {
                'state': 'live',
                'state_since': '2026-01-01T00:00:00Z',
                'metadata': {'note': 'do not touch'},
                'history': [],
                'instrument_class': 'equity',
            },
        },
        'decommissioned': {},
    }
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps(data, indent=2))
    return p


def _load(p: Path) -> dict:
    return json.loads(p.read_text())


def test_dry_run_writes_nothing(tmp_path, capsys):
    p = _fixture(tmp_path)
    before = p.read_bytes()
    before_mtime = p.stat().st_mtime_ns

    rc = hygiene.main(['--manifest', str(p), '--dry-run'])

    assert rc == 0
    assert p.read_bytes() == before
    assert p.stat().st_mtime_ns == before_mtime
    assert not (tmp_path / 'manifest.json.lock').exists()
    out = capsys.readouterr().out
    assert 'DRY-RUN' in out
    assert TR04 in out and TR06 in out


def test_dry_run_is_also_the_default_mode(tmp_path):
    p = _fixture(tmp_path)
    before = p.read_bytes()
    rc = hygiene.main(['--manifest', str(p)])   # no --dry-run / --apply flag
    assert rc == 0
    assert p.read_bytes() == before


def test_apply_makes_exactly_the_expected_changes(tmp_path, capsys):
    p = _fixture(tmp_path)

    rc = hygiene.main(['--manifest', str(p), '--apply'])
    assert rc == 0

    m = _load(p)
    for sid in hygiene.TARGETS:
        e = m['strategies'][sid]
        assert e['state'] == 'candidate'
        reasons = [ev['reason'] for ev in e['history']]
        assert hygiene.NEW_REASON in reasons
        assert hygiene.OLD_REASON not in reasons
        # Exactly one new transition event appended (archived -> candidate).
        assert len(e['history']) == 3
        last = e['history'][-1]
        assert last['from_state'] == 'archived'
        assert last['to_state'] == 'candidate'
        assert last['actor'] == 'manual:operator'
        assert last['reason'] == hygiene.REVIVAL_REASON
        assert e['backtest_quarantine'] == {
            'reason': hygiene.QUARANTINE_REASON, 'since': hygiene.REVIVAL_DATE,
        }
        assert e['metadata']['revival_2026_09_13'] == {
            'spec': hygiene.SPEC_REF,
            'quarantine_reason': hygiene.QUARANTINE_REASON,
            'owed': 'wire intraday 30m bars into aux_data',
        }
        # Original metadata preserved alongside the new key.
        assert e['metadata']['canonical_file'] == 'str04_zarattini_intraday_spy.py'

    # Control entry is byte-identical to the fixture's — untouched.
    assert m['strategies']['S_control_untouched'] == {
        'state': 'live', 'state_since': '2026-01-01T00:00:00Z',
        'metadata': {'note': 'do not touch'}, 'history': [], 'instrument_class': 'equity',
    }
    assert not (tmp_path / 'manifest.json.lock').exists()

    out = capsys.readouterr().out
    assert 'APPLY complete' in out
    assert f'{TR04}: applied' in out
    assert f'{TR06}: applied' in out


def test_apply_preserves_serialization_convention(tmp_path):
    """json.dumps(indent=2), no trailing newline, via write_atomic."""
    p = _fixture(tmp_path)
    hygiene.main(['--manifest', str(p), '--apply'])
    text = p.read_text()
    assert not text.endswith('\n')
    m = json.loads(text)
    assert json.dumps(m, indent=2) == text


def test_second_apply_is_a_noop(tmp_path, capsys):
    p = _fixture(tmp_path)
    assert hygiene.main(['--manifest', str(p), '--apply']) == 0
    after_first = p.read_bytes()
    capsys.readouterr()

    rc = hygiene.main(['--manifest', str(p), '--apply'])

    assert rc == 0
    assert p.read_bytes() == after_first
    out = capsys.readouterr().out
    assert f'{TR04}: no-op (already applied)' in out
    assert f'{TR06}: no-op (already applied)' in out


def test_dry_run_after_apply_reports_already_hygiened(tmp_path):
    p = _fixture(tmp_path)
    hygiene.main(['--manifest', str(p), '--apply'])
    before = p.read_bytes()

    rc = hygiene.main(['--manifest', str(p), '--dry-run'])

    assert rc == 0
    assert p.read_bytes() == before


def test_wrong_pre_state_refuses_and_writes_nothing(tmp_path, capsys):
    # TR04 already live (not archived, not our fully-applied shape either) —
    # an unexpected state a concurrent writer could have produced.
    p = _fixture(tmp_path, tr04=_target_entry(state='live'))
    before = p.read_bytes()

    rc = hygiene.main(['--manifest', str(p), '--apply'])

    assert rc == 1
    assert p.read_bytes() == before          # nothing written at all
    assert not (tmp_path / 'manifest.json.lock').exists()
    err = capsys.readouterr().err
    assert TR04 in err
    assert 'REFUSED' in err


def test_bad_precondition_on_one_target_leaves_the_other_unwritten(tmp_path, capsys):
    """Single locked read-modify-write covers both targets: TR04 (processed
    FIRST, per TARGETS order) is healthy and gets mutated in the in-memory
    dict; TR06 (processed second) is not. The exception raised for TR06
    must still block the write for BOTH — proving atomicity, not just
    short-circuiting on the first bad target."""
    p = _fixture(tmp_path, tr06=_target_entry(reason='some unrelated reason'))
    before = p.read_bytes()

    rc = hygiene.main(['--manifest', str(p), '--apply'])

    assert rc == 1
    m = _load(p)
    assert m == json.loads(before)
    assert m['strategies'][TR04]['state'] == 'archived'   # untouched, not revived
    assert m['strategies'][TR06]['state'] == 'archived'   # untouched, not revived


def test_dry_run_also_refuses_on_bad_precondition(tmp_path, capsys, monkeypatch):
    # Also exercises the POSTGRES_URI restore on the REFUSAL path: a naive
    # "restore after the loop" implementation would leak the popped env var
    # on this early `return 1` (inside the `except`), since the restore
    # lives in a `finally` specifically so it also runs here.
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake-host/fake-db')
    p = _fixture(tmp_path, tr06=_target_entry(state='deprecated'))
    before = p.read_bytes()

    rc = hygiene.main(['--manifest', str(p), '--dry-run'])

    assert rc == 1
    assert p.read_bytes() == before
    assert os.environ.get('POSTGRES_URI') == 'postgresql://fake-host/fake-db'  # restored
    err = capsys.readouterr().err
    assert TR06 in err


def _revived_entry_missing_quarantine() -> dict:
    """Simulates the KNOWN FRAGILITY: fully revived (state/reason/metadata
    correct) but `backtest_quarantine` was silently dropped by an unrelated
    auto_demote_negative_sharpe() -> save_manifest() elsewhere in the fleet
    (to_dict()'s fixed key set does not carry it). History shape mirrors a
    real post-`applied` entry: staging->candidate, candidate->archived
    (reason corrected), archived->candidate (the revival event) — 3 rows.
    """
    e = _target_entry(reason=hygiene.NEW_REASON, state='archived')
    e['history'].append({
        'from_state': 'archived', 'to_state': 'candidate',
        'timestamp': '2026-09-13T00:00:00+00:00', 'actor': 'manual:operator',
        'reason': hygiene.REVIVAL_REASON,
        'metadata': {'revival_2026_09_13': {
            'spec': hygiene.SPEC_REF, 'quarantine_reason': hygiene.QUARANTINE_REASON,
            'owed': 'wire intraday 30m bars into aux_data'}},
    })
    e['state'] = 'candidate'
    e['metadata']['revival_2026_09_13'] = {
        'spec': hygiene.SPEC_REF, 'quarantine_reason': hygiene.QUARANTINE_REASON,
        'owed': 'wire intraday 30m bars into aux_data',
    }
    # deliberately no 'backtest_quarantine' key
    return e


def test_requarantine_restores_only_the_dropped_flag(tmp_path, capsys):
    p = _fixture(tmp_path, tr04=_revived_entry_missing_quarantine(),
                 tr06=_revived_entry_missing_quarantine())

    rc = hygiene.main(['--manifest', str(p), '--apply'])

    assert rc == 0
    m = _load(p)
    for sid in hygiene.TARGETS:
        e = m['strategies'][sid]
        assert e['state'] == 'candidate'
        assert len(e['history']) == 3          # NOT re-transitioned — no 4th event
        assert e['backtest_quarantine'] == {
            'reason': hygiene.QUARANTINE_REASON, 'since': hygiene.REVIVAL_DATE,
        }
    out = capsys.readouterr().out
    assert f'{TR04}: requarantined' in out
    assert f'{TR06}: requarantined' in out

    # A follow-up apply is now a clean no-op.
    after_requarantine = p.read_bytes()
    assert hygiene.main(['--manifest', str(p), '--apply']) == 0
    assert p.read_bytes() == after_requarantine


def test_dry_run_never_touches_postgres_but_apply_still_persists(tmp_path, monkeypatch):
    """Fix round 1, item 1. POSTGRES_URI is set (mirroring main's default,
    where the '_no_postgres' autouse fixture would otherwise mask the real
    bug). --dry-run's 'pending'-phase preview builds a throwaway
    LifecycleStateMachine and calls .transition() purely to compute the
    diff — before the fix, that unconditionally reached
    _persist_lifecycle_event() and INSERTed into the real Postgres
    lifecycle_events table on every preview. Assert zero such calls from
    --dry-run, that POSTGRES_URI is restored to its pre-call value
    afterwards, and that a real --apply (same env) still persists exactly
    one call per revived strategy — the fix must not also break the real
    write path."""
    p = _fixture(tmp_path)
    calls = []

    def _recorder(self, strategy_id, event, metadata):
        # Mirrors the real _persist_lifecycle_event's own gate (it no-ops
        # without POSTGRES_URI) rather than bypassing it — transition()
        # calls this method unconditionally either way, so the thing under
        # test is whether POSTGRES_URI is actually present (i.e. whether a
        # real implementation would go on to open a Postgres connection),
        # not whether the Python method was invoked at all.
        if not os.environ.get('POSTGRES_URI'):
            return
        calls.append((strategy_id, event.from_state, event.to_state))

    monkeypatch.setattr(hygiene.LifecycleStateMachine, '_persist_lifecycle_event', _recorder)
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake-host/fake-db')

    rc = hygiene.main(['--manifest', str(p), '--dry-run'])

    assert rc == 0
    assert calls == []                                                       # zero DB calls
    assert os.environ.get('POSTGRES_URI') == 'postgresql://fake-host/fake-db'  # restored

    rc = hygiene.main(['--manifest', str(p), '--apply'])

    assert rc == 0
    assert len(calls) == 2                                                   # one per target
    assert {c[0] for c in calls} == set(hygiene.TARGETS)
    assert os.environ.get('POSTGRES_URI') == 'postgresql://fake-host/fake-db'


def test_dry_run_prints_byte_stability(tmp_path, capsys):
    p = _fixture(tmp_path)
    rc = hygiene.main(['--manifest', str(p), '--dry-run'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'byte-stable under write_atomic: True' in out   # fixture has no non-ASCII


def test_missing_manifest_file(tmp_path, capsys):
    missing = tmp_path / 'nope.json'
    rc = hygiene.main(['--manifest', str(missing), '--dry-run'])
    assert rc == 1
    assert 'no manifest' in capsys.readouterr().err
