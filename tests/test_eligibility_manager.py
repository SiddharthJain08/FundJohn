"""Tests for eligibility_manager — safe manifest edits with audit.

Run: pytest tests/test_eligibility_manager.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import eligibility_manager as em  # noqa: E402


@pytest.fixture
def manifest_path(tmp_path):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({
        'strategies': {
            'momentum_a': {'eligible_regimes': ['LOW_VOL', 'TRANSITIONING']},
            'mean_rev':   {'eligible_regimes': ['HIGH_VOL']},
            'no_field':   {},
        }
    }, indent=2))
    return p


def test_set_eligibility_updates_manifest(manifest_path, monkeypatch):
    audits: list = []
    monkeypatch.setattr(em, '_insert_audit', lambda **kw: audits.append(kw))
    em.set_eligibility(
        strategy_id='momentum_a',
        new_regimes=['LOW_VOL'],
        actor='operator:test',
        reason='live sharpe regression in TRANSITIONING',
        source='live_30d_sharpe=-0.5',
        manifest_path=manifest_path,
    )
    data = json.loads(manifest_path.read_text())
    assert data['strategies']['momentum_a']['eligible_regimes'] == ['LOW_VOL']
    assert len(audits) == 1
    assert audits[0]['before_regimes'] == ['LOW_VOL', 'TRANSITIONING']
    assert audits[0]['after_regimes'] == ['LOW_VOL']


def test_set_eligibility_rejects_invalid_regime(manifest_path):
    with pytest.raises(ValueError, match='invalid regime'):
        em.set_eligibility(
            strategy_id='momentum_a',
            new_regimes=['LOW_VOL', 'BOGUS'],
            actor='operator:test',
            reason='typo test',
            source='',
            manifest_path=manifest_path,
        )


def test_set_eligibility_rejects_unknown_strategy(manifest_path):
    with pytest.raises(KeyError):
        em.set_eligibility(
            strategy_id='does_not_exist',
            new_regimes=['LOW_VOL'],
            actor='operator:test',
            reason='', source='',
            manifest_path=manifest_path,
        )


def test_set_eligibility_rejects_empty_list(manifest_path):
    with pytest.raises(ValueError, match='at least one'):
        em.set_eligibility(
            strategy_id='momentum_a',
            new_regimes=[],
            actor='operator:test',
            reason='', source='',
            manifest_path=manifest_path,
        )


def test_set_eligibility_writes_atomically(manifest_path, monkeypatch):
    """If audit insert raises, manifest must not be left half-written."""
    def boom(**kw):
        raise RuntimeError('db down')
    monkeypatch.setattr(em, '_insert_audit', boom)
    with pytest.raises(RuntimeError):
        em.set_eligibility(
            strategy_id='momentum_a',
            new_regimes=['LOW_VOL'],
            actor='operator:test',
            reason='', source='',
            manifest_path=manifest_path,
        )
    # Manifest must be unchanged after rollback.
    data = json.loads(manifest_path.read_text())
    assert data['strategies']['momentum_a']['eligible_regimes'] == ['LOW_VOL', 'TRANSITIONING']


def test_list_strategies_returns_current_eligibility(manifest_path):
    out = em.list_strategies(manifest_path=manifest_path)
    by_id = {r['strategy_id']: r for r in out}
    assert by_id['momentum_a']['eligible_regimes'] == ['LOW_VOL', 'TRANSITIONING']
    assert by_id['no_field']['eligible_regimes'] is None  # backward-compat marker


def test_dedupe_and_sort_regimes(manifest_path, monkeypatch):
    monkeypatch.setattr(em, '_insert_audit', lambda **kw: None)
    em.set_eligibility(
        strategy_id='momentum_a',
        new_regimes=['HIGH_VOL', 'LOW_VOL', 'LOW_VOL', 'TRANSITIONING'],
        actor='operator:test', reason='', source='',
        manifest_path=manifest_path,
    )
    data = json.loads(manifest_path.read_text())
    assert data['strategies']['momentum_a']['eligible_regimes'] == \
        ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']  # canonical order, deduped


def test_recent_audit_returns_rows(monkeypatch):
    monkeypatch.setattr(em, '_query_audit', lambda limit: [
        {'changed_at': datetime.now(timezone.utc), 'strategy_id': 'momentum_a',
         'actor': 'cli', 'before_regimes': ['LOW_VOL', 'TRANSITIONING'],
         'after_regimes': ['LOW_VOL'], 'reason': 'test', 'source': ''},
    ])
    out = em.recent_audit(limit=10)
    assert len(out) == 1
    assert out[0]['strategy_id'] == 'momentum_a'
