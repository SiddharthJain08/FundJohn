"""Tests for doctor.check_regime_param_drift_alerts."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _stub_summary(monkeypatch, summary, raise_=None):
    def fake():
        if raise_:
            raise raise_
        return summary
    monkeypatch.setattr(doc, '_drift_summary', fake)


def test_no_alerts_returns_pass(monkeypatch):
    _stub_summary(monkeypatch, {'OK': 5, 'WARN': 0, 'FAIL': 0, 'INSUFFICIENT': 3})
    r = doc.check_regime_param_drift_alerts()
    assert r['severity'] == doc.PASS


def test_few_warn_returns_pass(monkeypatch):
    _stub_summary(monkeypatch, {'OK': 5, 'WARN': 2, 'FAIL': 0, 'INSUFFICIENT': 0})
    r = doc.check_regime_param_drift_alerts()
    assert r['severity'] == doc.PASS


def test_several_warn_returns_warn(monkeypatch):
    _stub_summary(monkeypatch, {'OK': 5, 'WARN': 5, 'FAIL': 0, 'INSUFFICIENT': 0})
    r = doc.check_regime_param_drift_alerts()
    assert r['severity'] == doc.WARN


def test_any_fail_returns_fail(monkeypatch):
    _stub_summary(monkeypatch, {'OK': 5, 'WARN': 0, 'FAIL': 1, 'INSUFFICIENT': 0})
    r = doc.check_regime_param_drift_alerts()
    assert r['severity'] == doc.FAIL


def test_many_warn_returns_fail(monkeypatch):
    _stub_summary(monkeypatch, {'OK': 0, 'WARN': 12, 'FAIL': 0, 'INSUFFICIENT': 0})
    r = doc.check_regime_param_drift_alerts()
    assert r['severity'] == doc.FAIL


def test_db_error_returns_warn(monkeypatch):
    _stub_summary(monkeypatch, {}, raise_=RuntimeError('db down'))
    r = doc.check_regime_param_drift_alerts()
    assert r['severity'] == doc.WARN
