"""Tests for Phase 2D doctor checks (calibration_brier + overlap_freshness)."""
from __future__ import annotations
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _stub_calibration(monkeypatch, total, brier, raise_=None):
    def fake():
        if raise_:
            raise raise_
        return {'total_observations': total, 'brier_score': brier, 'buckets': []}
    monkeypatch.setattr(doc, '_calibration_report', fake)


def test_calibration_insufficient_samples_returns_pass(monkeypatch):
    _stub_calibration(monkeypatch, total=3, brier=0.5)
    r = doc.check_mastermind_calibration_brier()
    assert r['severity'] == doc.PASS
    assert 'insufficient' in r['detail'].lower()


def test_calibration_good_brier_returns_pass(monkeypatch):
    _stub_calibration(monkeypatch, total=20, brier=0.05)
    r = doc.check_mastermind_calibration_brier()
    assert r['severity'] == doc.PASS


def test_calibration_mid_brier_returns_warn(monkeypatch):
    _stub_calibration(monkeypatch, total=20, brier=0.15)
    r = doc.check_mastermind_calibration_brier()
    assert r['severity'] == doc.WARN


def test_calibration_high_brier_returns_fail(monkeypatch):
    _stub_calibration(monkeypatch, total=20, brier=0.25)
    r = doc.check_mastermind_calibration_brier()
    assert r['severity'] == doc.FAIL


def test_calibration_db_error_returns_warn(monkeypatch):
    _stub_calibration(monkeypatch, 0, 0.0, raise_=RuntimeError('db down'))
    r = doc.check_mastermind_calibration_brier()
    assert r['severity'] == doc.WARN


def test_overlap_empty_returns_pass(monkeypatch):
    monkeypatch.setattr(doc, '_latest_overlap_run_at', lambda: None)
    r = doc.check_strategy_overlap_freshness()
    assert r['severity'] == doc.PASS


def test_overlap_fresh_returns_pass(monkeypatch):
    monkeypatch.setattr(doc, '_latest_overlap_run_at',
                        lambda: datetime.now(timezone.utc) - timedelta(hours=2))
    r = doc.check_strategy_overlap_freshness()
    assert r['severity'] == doc.PASS


def test_overlap_stale_returns_warn(monkeypatch):
    monkeypatch.setattr(doc, '_latest_overlap_run_at',
                        lambda: datetime.now(timezone.utc) - timedelta(hours=30))
    r = doc.check_strategy_overlap_freshness()
    assert r['severity'] == doc.WARN


def test_overlap_very_stale_returns_fail(monkeypatch):
    monkeypatch.setattr(doc, '_latest_overlap_run_at',
                        lambda: datetime.now(timezone.utc) - timedelta(hours=80))
    r = doc.check_strategy_overlap_freshness()
    assert r['severity'] == doc.FAIL
