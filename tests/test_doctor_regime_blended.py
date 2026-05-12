"""Tests for doctor.check_regime_blended_gate_b.

Run:
    pytest tests/test_doctor_regime_blended.py -v

Truth table covered (LIVE = OPENCLAW_REGIME_BLENDED_LIVE=1):
  LIVE=1, gate.pass=T, fresh  → PASS
  LIVE=1, gate.pass=T, stale  → FAIL   (no current evidence)
  LIVE=1, gate.pass=F, fresh  → FAIL   (gate broken)
  LIVE=1, gate.pass=F, stale  → FAIL
  LIVE=1, no backfill         → FAIL
  LIVE=0, gate.pass=T, fresh  → PASS
  LIVE=0, gate.pass=T, stale  → WARN
  LIVE=0, gate.pass=F, fresh  → WARN
  LIVE=0, no backfill         → WARN
  walk-forward crash          → WARN (cannot evaluate)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _result(*, gate_pass, sharpe_delta=-0.13, max_dd_delta=0.0,
            run_at=None):
    if run_at is None:
        run_at = datetime.now(timezone.utc)
    return {
        'source': 'strategy_regime_backtests',
        'run_id': 'fake-run',
        'run_at': run_at.isoformat(),
        'strategy_count': 53,
        'delta': {'sharpe': sharpe_delta, 'max_dd': max_dd_delta},
        'gate_b': {
            'sharpe_regression_tolerance': 0.25,
            'sharpe_within_tolerance': abs(sharpe_delta) <= 0.25 or sharpe_delta > 0,
            'sharpe_positive': sharpe_delta > 0,
            'max_dd_not_worse': max_dd_delta <= 0,
            'low_vol_strategies_present': True,
            'pass': gate_pass,
        },
    }


def _stub_harness(monkeypatch, result):
    monkeypatch.setattr('backtest.regime_blended_backtest.run_walkforward',
                        lambda *a, **kw: result)


def test_live_pass_fresh_returns_ok(monkeypatch):
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    _stub_harness(monkeypatch, _result(gate_pass=True))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.PASS
    assert 'live=True' in r['detail']


def test_live_pass_stale_returns_fail(monkeypatch):
    """LIVE + pass + stale: NO current evidence the gate still passes."""
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    stale_at = datetime.now(timezone.utc) - timedelta(days=20)
    _stub_harness(monkeypatch, _result(gate_pass=True, run_at=stale_at))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.FAIL
    assert 'stale' in r['detail'].lower()


def test_live_fail_fresh_returns_fail(monkeypatch):
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    _stub_harness(monkeypatch, _result(gate_pass=False, sharpe_delta=-0.5))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.FAIL
    assert 'gate.pass=false' in r['detail']


def test_live_fail_stale_returns_fail(monkeypatch):
    """Both conditions failing: reasons concatenated."""
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    stale_at = datetime.now(timezone.utc) - timedelta(days=30)
    _stub_harness(monkeypatch, _result(gate_pass=False, sharpe_delta=-0.5,
                                        run_at=stale_at))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.FAIL
    assert 'gate.pass=false' in r['detail']
    assert 'stale' in r['detail'].lower()


def test_live_no_backfill_returns_fail(monkeypatch):
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    _stub_harness(monkeypatch, {'error': 'strategy_regime_backtests has no rows — run backfill first'})
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.FAIL
    assert 'no backtests' in r['detail'].lower() or 'no rows' in r['detail'].lower()


def test_dry_run_pass_fresh_returns_ok(monkeypatch):
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    _stub_harness(monkeypatch, _result(gate_pass=True))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.PASS
    assert 'live=False' in r['detail']


def test_dry_run_pass_stale_returns_warn(monkeypatch):
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    stale_at = datetime.now(timezone.utc) - timedelta(days=20)
    _stub_harness(monkeypatch, _result(gate_pass=True, run_at=stale_at))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.WARN
    assert 'stale' in r['detail'].lower()


def test_dry_run_fail_fresh_returns_warn(monkeypatch):
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    _stub_harness(monkeypatch, _result(gate_pass=False, sharpe_delta=-0.5))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.WARN


def test_dry_run_no_backfill_returns_warn(monkeypatch):
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    _stub_harness(monkeypatch, {'error': 'strategy_regime_backtests has no rows — run backfill first'})
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.WARN


def test_walk_forward_exception_returns_warn_even_in_live(monkeypatch):
    """Infra failure ≠ gate failure. WARN so operator investigates separately."""
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')

    def raise_(*a, **kw):
        raise RuntimeError('db down')

    monkeypatch.setattr('backtest.regime_blended_backtest.run_walkforward', raise_)
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.WARN
    assert 'walk-forward failed' in r['detail']
