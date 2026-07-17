"""Regression: lifecycle.py must (a) round-trip top-level instrument_class,
(b) backfill absent instrument_class to 'equity', (c) never strip another
strategy's instrument_class during an unrelated promotion.

Run: pytest tests/test_lifecycle_instrument_class.py -v
"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from strategies.lifecycle import LifecycleStateMachine  # noqa: E402


def _write(tmp_path, payload):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return p


def _entry(state='live', **extra):
    e = {'state': state, 'state_since': '2026-05-01T00:00:00Z',
         'metadata': {}, 'history': []}
    e.update(extra)
    return e


def test_declared_instrument_class_round_trips(tmp_path):
    p = _write(tmp_path, {'strategies': {'s1': _entry(instrument_class='etp')}})
    out = LifecycleStateMachine.from_manifest(p).to_dict()
    assert out['strategies']['s1']['instrument_class'] == 'etp'


def test_absent_instrument_class_backfills_equity(tmp_path):
    p = _write(tmp_path, {'strategies': {'s1': _entry()}})
    out = LifecycleStateMachine.from_manifest(p).to_dict()
    assert out['strategies']['s1']['instrument_class'] == 'equity'


def test_save_manifest_preserves_instrument_class(tmp_path):
    p = _write(tmp_path, {'strategies': {'s1': _entry(instrument_class='etp')}})
    sm = LifecycleStateMachine.from_manifest(p)
    sm.save_manifest(p)
    reloaded = json.loads(p.read_text(encoding='utf-8'))
    assert reloaded['strategies']['s1']['instrument_class'] == 'etp'


def test_unrelated_promotion_does_not_strip_instrument_class(tmp_path):
    p = _write(tmp_path, {'strategies': {
        's1': _entry(instrument_class='etp'),
        's2': _entry(state='staging'),
    }})
    sm = LifecycleStateMachine.from_manifest(p)
    sm.get_record('s2').state_since = '2026-05-12T19:25:00Z'
    sm.save_manifest(p)
    reloaded = json.loads(p.read_text(encoding='utf-8'))
    assert reloaded['strategies']['s1']['instrument_class'] == 'etp', \
        'promotion of s2 must not strip s1.instrument_class'


import pytest  # noqa: E402

def test_unknown_instrument_class_rejected(tmp_path):
    p = _write(tmp_path, {'strategies': {'s1': _entry(instrument_class='banana')}})
    with pytest.raises(ValueError, match='instrument_class'):
        LifecycleStateMachine.from_manifest(p)


def test_reserved_classes_accepted(tmp_path):
    for cls in ('crypto', 'futures'):
        p = _write(tmp_path, {'strategies': {'s1': _entry(instrument_class=cls)}})
        out = LifecycleStateMachine.from_manifest(p).to_dict()
        assert out['strategies']['s1']['instrument_class'] == cls
