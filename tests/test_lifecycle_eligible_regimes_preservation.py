"""Regression: lifecycle.py must preserve top-level eligible_regimes across
the from_manifest → to_dict round-trip. Discovered 2026-05-12 when a
background lifecycle promotion (state staging → live for one strategy)
stripped operator eligibility decisions for FOUR other strategies.

Run: pytest tests/test_lifecycle_eligible_regimes_preservation.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies.lifecycle import LifecycleStateMachine  # noqa: E402


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return p


def test_top_level_eligible_regimes_round_trips(tmp_path):
    """A strategy with top-level eligible_regimes must keep it through
    from_manifest → to_dict."""
    manifest = {
        'schema_version': '1.0',
        'updated_at': '2026-05-12T00:00:00Z',
        'strategies': {
            's1': {
                'state': 'live',
                'state_since': '2026-05-01T00:00:00Z',
                'metadata': {'class': 'Foo'},
                'history': [],
                'eligible_regimes': ['LOW_VOL', 'TRANSITIONING'],
            },
        },
    }
    p = _write_manifest(tmp_path, manifest)
    sm = LifecycleStateMachine.from_manifest(p)
    out = sm.to_dict()
    assert out['strategies']['s1']['eligible_regimes'] == ['LOW_VOL', 'TRANSITIONING']


def test_strategy_without_eligible_regimes_omits_field(tmp_path):
    """Legacy strategies (no eligible_regimes field) must not get one added."""
    manifest = {
        'schema_version': '1.0',
        'updated_at': '2026-05-12T00:00:00Z',
        'strategies': {
            's1': {
                'state': 'live',
                'state_since': '2026-05-01T00:00:00Z',
                'metadata': {},
                'history': [],
            },
        },
    }
    p = _write_manifest(tmp_path, manifest)
    sm = LifecycleStateMachine.from_manifest(p)
    out = sm.to_dict()
    assert 'eligible_regimes' not in out['strategies']['s1']


def test_save_manifest_preserves_eligible_regimes(tmp_path):
    """Full save_manifest cycle (with the cross-process lock) must
    preserve eligible_regimes."""
    manifest = {
        'schema_version': '1.0',
        'updated_at': '2026-05-12T00:00:00Z',
        'strategies': {
            's1': {
                'state': 'live',
                'state_since': '2026-05-01T00:00:00Z',
                'metadata': {},
                'history': [],
                'eligible_regimes': ['HIGH_VOL', 'CRISIS'],
            },
        },
    }
    p = _write_manifest(tmp_path, manifest)
    sm = LifecycleStateMachine.from_manifest(p)
    sm.save_manifest(p)
    reloaded = json.loads(p.read_text(encoding='utf-8'))
    assert reloaded['strategies']['s1']['eligible_regimes'] == ['HIGH_VOL', 'CRISIS']


def test_eligibility_set_by_other_writer_is_preserved_through_lifecycle_promotion(tmp_path):
    """The original bug scenario:
       1. Manifest has eligible_regimes for s1 set by operator UI (via eligibility_manager.py).
       2. Lifecycle.from_manifest reads, modifies s2 (state transition).
       3. save_manifest writes back.
       4. s1's eligible_regimes MUST survive — even though lifecycle never
          knew it existed at the dataclass level."""
    manifest = {
        'schema_version': '1.0',
        'updated_at': '2026-05-12T00:00:00Z',
        'strategies': {
            's1': {
                'state': 'live',
                'state_since': '2026-05-01T00:00:00Z',
                'metadata': {},
                'history': [],
                'eligible_regimes': ['LOW_VOL'],  # operator decision
            },
            's2': {
                'state': 'staging',
                'state_since': '2026-05-01T00:00:00Z',
                'metadata': {},
                'history': [],
            },
        },
    }
    p = _write_manifest(tmp_path, manifest)
    sm = LifecycleStateMachine.from_manifest(p)

    # Simulate a lifecycle promotion that touches a different strategy.
    rec_s2 = sm.get_record('s2')
    rec_s2.state_since = '2026-05-12T19:25:00Z'  # something modifying s2

    sm.save_manifest(p)
    reloaded = json.loads(p.read_text(encoding='utf-8'))
    assert reloaded['strategies']['s1']['eligible_regimes'] == ['LOW_VOL'], (
        'lifecycle promotion of s2 must not strip s1.eligible_regimes')
