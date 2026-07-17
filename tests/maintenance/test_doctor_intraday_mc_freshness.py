"""Tests for Phase 2E doctor intraday_mc_freshness check."""
from __future__ import annotations
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _stub(monkeypatch, latest, stale_pending=0):
    monkeypatch.setattr(doc, '_latest_intraday_mc_run_at', lambda: latest)
    monkeypatch.setattr(doc, '_count_pending_proposals_without_intraday_mc',
                          lambda days: stale_pending)


def test_intraday_mc_empty_table_is_pass(monkeypatch):
    _stub(monkeypatch, latest=None, stale_pending=0)
    r = doc.check_intraday_mc_freshness()
    assert r['severity'] == doc.PASS


def test_intraday_mc_fresh_is_pass(monkeypatch):
    _stub(monkeypatch, latest=datetime.now(timezone.utc) - timedelta(hours=2))
    r = doc.check_intraday_mc_freshness()
    assert r['severity'] == doc.PASS


def test_intraday_mc_stale_is_warn(monkeypatch):
    _stub(monkeypatch, latest=datetime.now(timezone.utc) - timedelta(hours=30))
    r = doc.check_intraday_mc_freshness()
    assert r['severity'] == doc.WARN


def test_intraday_mc_very_stale_no_pending_is_warn(monkeypatch):
    """Stale > 72h without pending proposals → WARN (2026-05-19: demoted
    from FAIL under the regime-redeploy-not-liquidate architecture; the
    daily cycle does not consume path-MC, so loud aborts here only mask
    the real signal which is `test_intraday_mc_stale_pending_proposal_is_fail`)."""
    _stub(monkeypatch, latest=datetime.now(timezone.utc) - timedelta(hours=80))
    r = doc.check_intraday_mc_freshness()
    assert r['severity'] == doc.WARN
    assert 'no pending' in r['detail']


def test_intraday_mc_stale_pending_proposal_is_fail(monkeypatch):
    """Pending proposal aged > 7d without a path-MC row → FAIL (gating on
    decision support, not just data staleness)."""
    _stub(monkeypatch,
          latest=datetime.now(timezone.utc) - timedelta(hours=2),
          stale_pending=3)
    r = doc.check_intraday_mc_freshness()
    assert r['severity'] == doc.FAIL
    assert '3' in r['detail']


def test_intraday_mc_db_error_returns_warn(monkeypatch):
    def boom():
        raise RuntimeError('db down')
    monkeypatch.setattr(doc, '_latest_intraday_mc_run_at', boom)
    r = doc.check_intraday_mc_freshness()
    assert r['severity'] == doc.WARN
