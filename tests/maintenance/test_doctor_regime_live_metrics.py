"""Tests for doctor.check_regime_live_rollup_freshness and
doctor.check_manifest_eligibility_drift.

Run: pytest tests/test_doctor_regime_live_metrics.py -v
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


# ── rollup freshness ─────────────────────────────────────────────────────

def _stub_latest_run(monkeypatch, run_at):
    monkeypatch.setattr(doc, '_latest_rollup_run_at', lambda uri: run_at)


def test_fresh_rollup_returns_pass(monkeypatch):
    _stub_latest_run(monkeypatch, datetime.now(timezone.utc) - timedelta(hours=2))
    r = doc.check_regime_live_rollup_freshness()
    assert r['severity'] == doc.PASS


def test_stale_rollup_returns_warn(monkeypatch):
    _stub_latest_run(monkeypatch, datetime.now(timezone.utc) - timedelta(hours=30))
    r = doc.check_regime_live_rollup_freshness()
    assert r['severity'] == doc.WARN
    assert 'stale' in r['detail'].lower()


def test_very_stale_rollup_returns_fail(monkeypatch):
    _stub_latest_run(monkeypatch, datetime.now(timezone.utc) - timedelta(hours=80))
    r = doc.check_regime_live_rollup_freshness()
    assert r['severity'] == doc.FAIL


def test_empty_rollup_returns_fail(monkeypatch):
    _stub_latest_run(monkeypatch, None)
    r = doc.check_regime_live_rollup_freshness()
    assert r['severity'] == doc.FAIL
    assert ('empty' in r['detail'].lower()
            or 'no rollup' in r['detail'].lower()
            or 'never run' in r['detail'].lower())


def test_db_error_returns_warn(monkeypatch):
    def raise_(uri): raise RuntimeError('db down')
    monkeypatch.setattr(doc, '_latest_rollup_run_at', raise_)
    r = doc.check_regime_live_rollup_freshness()
    assert r['severity'] == doc.WARN


# ── manifest drift (Phase 2A: detects writes to deprecated eligible_regimes) ──

import json
from pathlib import Path


def _write_repo_manifest(tmp_path, payload):
    repo = tmp_path / 'repo'
    (repo / 'src' / 'strategies').mkdir(parents=True)
    (repo / 'src' / 'strategies' / 'manifest.json').write_text(
        json.dumps(payload), encoding='utf-8')
    return repo


def test_manifest_no_deprecated_field_returns_pass(monkeypatch, tmp_path):
    repo = _write_repo_manifest(tmp_path, {'strategies': {
        's1': {'state': 'live', 'metadata': {}},
    }})
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(repo))
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.PASS


def test_manifest_with_deprecated_field_returns_warn(monkeypatch, tmp_path):
    repo = _write_repo_manifest(tmp_path, {'strategies': {
        's1': {'state': 'live', 'eligible_regimes': ['LOW_VOL']},
    }})
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(repo))
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 's1' in r['detail']


def test_manifest_with_deprecated_field_in_live_returns_fail(monkeypatch, tmp_path):
    repo = _write_repo_manifest(tmp_path, {'strategies': {
        's1': {'state': 'live', 'eligible_regimes': ['LOW_VOL']},
    }})
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(repo))
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.FAIL


def test_unparseable_manifest_returns_warn(monkeypatch, tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'src' / 'strategies').mkdir(parents=True)
    (repo / 'src' / 'strategies' / 'manifest.json').write_text('not json{',
                                                                  encoding='utf-8')
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(repo))
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN


def test_missing_working_manifest_returns_warn(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(tmp_path))
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 'unreadable' in r['detail'].lower()
