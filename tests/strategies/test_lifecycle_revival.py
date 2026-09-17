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
