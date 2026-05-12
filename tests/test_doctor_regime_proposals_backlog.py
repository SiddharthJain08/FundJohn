"""Tests for doctor.check_regime_proposals_backlog."""
from __future__ import annotations
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _stub_pending(monkeypatch, ages_days, raise_=None):
    """ages_days: list of integers — age (in days) of each pending proposal."""
    def fake_query(sql, params=()):
        if raise_:
            raise raise_
        now = datetime.now(timezone.utc)
        # Return rows shaped as (proposed_at,) for the count-by-age query
        return [(now - timedelta(days=d),) for d in ages_days]
    monkeypatch.setattr(doc, '_query_proposals_backlog', fake_query)


def test_no_pending_returns_pass(monkeypatch):
    _stub_pending(monkeypatch, [])
    r = doc.check_regime_proposals_backlog()
    assert r['severity'] == doc.PASS


def test_fresh_pending_returns_pass(monkeypatch):
    _stub_pending(monkeypatch, [1, 3, 6])
    r = doc.check_regime_proposals_backlog()
    assert r['severity'] == doc.PASS


def test_aged_pending_returns_warn(monkeypatch):
    _stub_pending(monkeypatch, [15, 20])
    r = doc.check_regime_proposals_backlog()
    assert r['severity'] == doc.WARN


def test_very_aged_pending_returns_fail(monkeypatch):
    _stub_pending(monkeypatch, [35])
    r = doc.check_regime_proposals_backlog()
    assert r['severity'] == doc.FAIL


def test_many_aged_pending_returns_fail(monkeypatch):
    _stub_pending(monkeypatch, [15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25])
    r = doc.check_regime_proposals_backlog()
    assert r['severity'] == doc.FAIL


def test_db_error_returns_warn(monkeypatch):
    _stub_pending(monkeypatch, [], raise_=RuntimeError('db down'))
    r = doc.check_regime_proposals_backlog()
    assert r['severity'] == doc.WARN
