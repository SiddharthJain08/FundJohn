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


# ── manifest drift ───────────────────────────────────────────────────────

def test_manifest_in_sync_with_head_returns_pass(monkeypatch):
    """git diff exits 0 with empty output → no drift."""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.PASS


def test_manifest_drift_returns_warn(monkeypatch):
    """git diff shows eligible_regimes changes → WARN in DRY-RUN.

    `git diff` exits 0 in both no-change and change cases; non-zero means
    error. Diff content is in stdout.
    """
    diff_out = (
        '+        "eligible_regimes": ["TRANSITIONING"],\n'
        '-        "eligible_regimes": ["LOW_VOL", "TRANSITIONING"],'
    )

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=diff_out, stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 'eligible_regimes' in r['detail']


def test_manifest_drift_in_live_returns_fail(monkeypatch):
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    diff_out = '+        "eligible_regimes": ["TRANSITIONING"],'

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=diff_out, stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.FAIL


def test_manifest_drift_git_unavailable_returns_warn(monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError('git not found')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN


def test_manifest_drift_git_error_returns_warn(monkeypatch):
    """git exits non-zero (e.g. dubious ownership) — must not silently PASS."""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 128, stdout='',
            stderr="fatal: detected dubious ownership in repository at '/root/openclaw'")
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 'git diff failed' in r['detail'] or 'dubious' in r['detail'].lower()
