"""tests/test_regime_gate.py

Unit tests for src/strategies/regime_gate.py (per-strategy regime-eligibility
gate). Tests the is_eligible(strategy_id, regime_state) function which is
called by engine.run_strategies() before invoking each strategy's
compute_signals().

Run:
    pytest tests/test_regime_gate.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies.regime_gate import is_eligible, ALL_REGIMES  # noqa: E402


def _write_manifest(tmp_path, strategies):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': strategies}))
    return p


def test_explicit_eligible_match(tmp_path, monkeypatch):
    p = _write_manifest(tmp_path, {'S1': {'state': 'live', 'eligible_regimes': ['LOW_VOL', 'HIGH_VOL']}})
    monkeypatch.setattr('strategies.regime_gate.MANIFEST_PATH', p)
    assert is_eligible('S1', 'LOW_VOL') is True
    assert is_eligible('S1', 'HIGH_VOL') is True


def test_explicit_eligible_miss(tmp_path, monkeypatch):
    p = _write_manifest(tmp_path, {'S1': {'state': 'live', 'eligible_regimes': ['LOW_VOL']}})
    monkeypatch.setattr('strategies.regime_gate.MANIFEST_PATH', p)
    assert is_eligible('S1', 'CRISIS') is False


def test_missing_field_defaults_all_regimes(tmp_path, monkeypatch):
    p = _write_manifest(tmp_path, {'S1': {'state': 'live'}})
    monkeypatch.setattr('strategies.regime_gate.MANIFEST_PATH', p)
    for r in ALL_REGIMES:
        assert is_eligible('S1', r) is True


def test_malformed_eligible_defaults_all_regimes(tmp_path, monkeypatch, caplog):
    p = _write_manifest(tmp_path, {'S1': {'state': 'live', 'eligible_regimes': 'not-a-list'}})
    monkeypatch.setattr('strategies.regime_gate.MANIFEST_PATH', p)
    assert is_eligible('S1', 'LOW_VOL') is True
    assert any('malformed' in rec.message.lower() for rec in caplog.records)


def test_unknown_strategy_defaults_all_regimes(tmp_path, monkeypatch):
    p = _write_manifest(tmp_path, {'S1': {'state': 'live', 'eligible_regimes': ['LOW_VOL']}})
    monkeypatch.setattr('strategies.regime_gate.MANIFEST_PATH', p)
    assert is_eligible('UNKNOWN', 'LOW_VOL') is True


def test_empty_eligible_list_blocks_all(tmp_path, monkeypatch):
    p = _write_manifest(tmp_path, {'S1': {'state': 'live', 'eligible_regimes': []}})
    monkeypatch.setattr('strategies.regime_gate.MANIFEST_PATH', p)
    for r in ALL_REGIMES:
        assert is_eligible('S1', r) is False


def test_invalid_regime_in_list_logs_warning(tmp_path, monkeypatch, caplog):
    p = _write_manifest(tmp_path, {'S1': {'state': 'live', 'eligible_regimes': ['LOW_VOL', 'TYPO']}})
    monkeypatch.setattr('strategies.regime_gate.MANIFEST_PATH', p)
    assert is_eligible('S1', 'LOW_VOL') is True
    assert is_eligible('S1', 'TYPO') is False
    assert any('TYPO' in rec.message for rec in caplog.records)
