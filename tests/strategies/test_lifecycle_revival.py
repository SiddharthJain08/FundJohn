# tests/strategies/test_lifecycle_revival.py
"""archived -> candidate: reviving a strategy whose blocking data gap closed
(spec 2026-09-12 §A4 hygiene). Operates on a tmp_path manifest copy only."""
from __future__ import annotations

import json

import pytest

from strategies.lifecycle import LifecycleStateMachine, LifecycleError, StrategyState, VALID_TRANSITIONS


def _manifest(tmp_path):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {
        'S_revive_me': {
            'state': 'archived',
            'state_since': '2026-04-30T23:37:01.649Z',
            'metadata': {'canonical_file': 'str04_zarattini_intraday_spy.py',
                         'class': 'ZarattiniIntradaySpy'},
            'history': [],
            'instrument_class': 'equity',
        },
    }}))
    return p


def test_archived_to_candidate_is_a_valid_transition():
    assert (StrategyState.ARCHIVED, StrategyState.CANDIDATE) in VALID_TRANSITIONS


def test_revival_moves_state_and_appends_history(tmp_path, monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)   # _persist_lifecycle_event no-ops
    p = _manifest(tmp_path)
    lsm = LifecycleStateMachine.from_manifest(str(p))
    rec = lsm.transition('S_revive_me', StrategyState.CANDIDATE,
                         actor='manual:operator', reason='data gap closed')
    assert rec.state is StrategyState.CANDIDATE
    assert rec.history[-1].from_state == 'archived'
    assert rec.history[-1].to_state == 'candidate'
    lsm.save_manifest(str(p))
    assert json.loads(p.read_text())['strategies']['S_revive_me']['state'] == 'candidate'


def test_archived_to_live_is_still_refused(tmp_path, monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    lsm = LifecycleStateMachine.from_manifest(str(_manifest(tmp_path)))
    with pytest.raises(LifecycleError):
        lsm.transition('S_revive_me', StrategyState.LIVE, actor='test')


def test_backtest_quarantine_survives_from_manifest_and_save(tmp_path, monkeypatch):
    """Fix round 1, item 3 (RULED). Before this fix, to_dict()'s fixed key
    set silently dropped a top-level `backtest_quarantine` flag on ANY
    save_manifest() call — including one triggered by an UNRELATED
    strategy's transition, e.g. auto_demote_negative_sharpe() demoting some
    other strategy elsewhere in the fleet then calling save_manifest(),
    which (via to_dict()) re-emits every loaded record, not just the one
    that transitioned. scripts/refresh_backtests_resumable.js reads this
    flag at the top level (`!e.backtest_quarantine`) to skip a strategy
    from the nightly work queue, so losing it silently re-queues a strategy
    that was deliberately parked. A manifest entry that never carried the
    flag must gain no key on save (byte-identical round-trip)."""
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    p = tmp_path / 'manifest.json'
    quarantine = {'reason': 'no prices_30m in backtest aux', 'since': '2026-09-13'}
    p.write_text(json.dumps({'strategies': {
        'S_quarantined': {
            'state': 'candidate',
            'state_since': '2026-09-13T00:00:00+00:00',
            'metadata': {},
            'history': [],
            'instrument_class': 'equity',
            'backtest_quarantine': quarantine,
        },
        'S_plain': {
            'state': 'candidate',
            'state_since': '2026-09-13T00:00:00+00:00',
            'metadata': {},
            'history': [],
            'instrument_class': 'equity',
        },
    }}))

    lsm = LifecycleStateMachine.from_manifest(str(p))
    # An UNRELATED transition on a different strategy — the scenario that
    # used to wipe S_quarantined's flag even though nothing about it changed.
    # (candidate -> archived: "abandon without going live" — no extra
    # metadata guard, unlike candidate -> staging's regime-eligibility check.)
    lsm.transition('S_plain', StrategyState.ARCHIVED, actor='test', reason='unrelated')
    lsm.save_manifest(str(p))

    saved = json.loads(p.read_text())['strategies']
    assert saved['S_quarantined']['backtest_quarantine'] == quarantine
    assert 'backtest_quarantine' not in saved['S_plain']
