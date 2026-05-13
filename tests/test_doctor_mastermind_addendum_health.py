"""Tests for Phase 2F doctor mastermind_addendum_health check."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _stub(monkeypatch, expired_but_active=0, stale_pending=0, total_active=0):
    monkeypatch.setattr(doc, '_query_addenda_health',
                          lambda: {'expired_but_active': expired_but_active,
                                    'stale_pending':       stale_pending,
                                    'total_active':        total_active})


def test_addendum_health_clean_state_is_pass(monkeypatch):
    _stub(monkeypatch)
    r = doc.check_mastermind_addendum_health()
    assert r['severity'] == doc.PASS


def test_addendum_health_one_expired_but_active_is_warn(monkeypatch):
    _stub(monkeypatch, expired_but_active=1, total_active=2)
    r = doc.check_mastermind_addendum_health()
    assert r['severity'] == doc.WARN


def test_addendum_health_three_expired_is_fail(monkeypatch):
    _stub(monkeypatch, expired_but_active=3)
    r = doc.check_mastermind_addendum_health()
    assert r['severity'] == doc.FAIL


def test_addendum_health_stale_pending_is_warn(monkeypatch):
    _stub(monkeypatch, stale_pending=1)
    r = doc.check_mastermind_addendum_health()
    assert r['severity'] == doc.WARN


def test_addendum_health_many_stale_pending_is_fail(monkeypatch):
    _stub(monkeypatch, stale_pending=3)
    r = doc.check_mastermind_addendum_health()
    assert r['severity'] == doc.FAIL


def test_addendum_health_db_error_returns_warn(monkeypatch):
    def boom():
        raise RuntimeError('db gone')
    monkeypatch.setattr(doc, '_query_addenda_health', boom)
    r = doc.check_mastermind_addendum_health()
    assert r['severity'] == doc.WARN
