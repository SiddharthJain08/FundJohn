"""Tests for doctor.check_regime_live_rollup_freshness and
doctor.check_manifest_eligibility_drift.

Run: pytest tests/test_doctor_regime_live_metrics.py -v
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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


# ── manifest drift (state-aware, JSON-level comparison) ───────────────────

import json
from pathlib import Path


def _stub_manifests(monkeypatch, head_obj, working_obj, tmp_path):
    """Stub `git show HEAD:...` to return JSON-serialized head_obj, and
    point check_manifest_eligibility_drift at a temp working-tree path
    by setting OPENCLAW_REPO_ROOT and writing the working manifest there.
    """
    repo_root = tmp_path / 'repo'
    (repo_root / 'src' / 'strategies').mkdir(parents=True)
    (repo_root / 'src' / 'strategies' / 'manifest.json').write_text(
        json.dumps(working_obj), encoding='utf-8')
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(repo_root))

    head_blob = json.dumps(head_obj)

    def fake_run(cmd, **kw):
        # We only stub `git show HEAD:src/strategies/manifest.json`
        return subprocess.CompletedProcess(cmd, 0, stdout=head_blob, stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)


def test_manifest_in_sync_with_head_returns_pass(monkeypatch, tmp_path):
    """Same eligible_regimes on every shared strategy."""
    head = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL', 'TRANSITIONING']},
        's2': {'eligible_regimes': None},
    }}
    wrk = json.loads(json.dumps(head))  # deep copy
    _stub_manifests(monkeypatch, head, wrk, tmp_path)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.PASS


def test_existing_strategy_eligibility_changed_returns_warn(monkeypatch, tmp_path):
    head = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL', 'TRANSITIONING']},
    }}
    wrk = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL']},  # operator trimmed TRANSITIONING
    }}
    _stub_manifests(monkeypatch, head, wrk, tmp_path)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 's1' in r['detail']


def test_existing_strategy_first_eligibility_set_returns_warn(monkeypatch, tmp_path):
    """HEAD had no eligible_regimes field; working tree adds one → real drift."""
    head = {'strategies': {
        's1': {},  # no eligible_regimes field
    }}
    wrk = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL', 'TRANSITIONING']},
    }}
    _stub_manifests(monkeypatch, head, wrk, tmp_path)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 's1' in r['detail']


def test_new_strategy_addition_not_flagged(monkeypatch, tmp_path):
    """Strategy only in working tree (never been in HEAD) → not drift."""
    head = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL']},
    }}
    wrk = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL']},  # unchanged
        'brand_new': {'eligible_regimes': ['HIGH_VOL']},  # new addition
    }}
    _stub_manifests(monkeypatch, head, wrk, tmp_path)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.PASS
    assert 'brand_new' not in r['detail']


def test_removed_strategy_not_flagged(monkeypatch, tmp_path):
    """Strategy removed from working tree → not eligibility drift (different concern)."""
    head = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL']},
        's2': {'eligible_regimes': ['HIGH_VOL']},
    }}
    wrk = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL']},
    }}
    _stub_manifests(monkeypatch, head, wrk, tmp_path)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.PASS


def test_existing_drift_in_live_returns_fail(monkeypatch, tmp_path):
    head = {'strategies': {'s1': {'eligible_regimes': ['LOW_VOL', 'TRANSITIONING']}}}
    wrk  = {'strategies': {'s1': {'eligible_regimes': ['LOW_VOL']}}}
    _stub_manifests(monkeypatch, head, wrk, tmp_path)
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.FAIL


def test_manifest_drift_git_unavailable_returns_warn(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        raise FileNotFoundError('git not found')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 'git unavailable' in r['detail']


def test_manifest_drift_git_error_returns_warn(monkeypatch, tmp_path):
    """git exits non-zero (e.g. dubious ownership) → WARN, not silent PASS."""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 128, stdout='',
            stderr="fatal: detected dubious ownership in repository at '/root/openclaw'")
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 'git show' in r['detail'].lower() or 'dubious' in r['detail'].lower()


def test_unparseable_head_manifest_returns_warn(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout='not json{', stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(tmp_path))
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 'unparseable' in r['detail'].lower() or 'unreadable' in r['detail'].lower()


def test_missing_working_manifest_returns_warn(monkeypatch, tmp_path):
    head = {'strategies': {}}
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(head), stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    # Point repo_root at empty dir — working manifest doesn't exist.
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(tmp_path))
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 'unreadable' in r['detail'].lower()
