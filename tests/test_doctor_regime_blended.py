"""Tests for doctor.check_regime_blended_gate_b.

Run:
    pytest tests/test_doctor_regime_blended.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _result(*, gate_pass, sharpe_delta=-0.13, max_dd_delta=0.0):
    return {
        'source': 'strategy_regime_backtests',
        'run_id': 'fake-run',
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


def test_pass_in_dry_run_returns_ok(monkeypatch):
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    monkeypatch.setattr('backtest.regime_blended_backtest.run_walkforward',
                        lambda *a, **kw: _result(gate_pass=True))
    # Freshness DB probe — stub it to a recent timestamp.
    monkeypatch.setattr('psycopg2.connect', _FakeConn(recent=True))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.PASS
    assert 'live=False' in r['detail']


def test_pass_in_live_returns_ok(monkeypatch):
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    monkeypatch.setattr('backtest.regime_blended_backtest.run_walkforward',
                        lambda *a, **kw: _result(gate_pass=True))
    monkeypatch.setattr('psycopg2.connect', _FakeConn(recent=True))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.PASS
    assert 'live=True' in r['detail']


def test_fail_in_dry_run_returns_warn(monkeypatch):
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    monkeypatch.setattr('backtest.regime_blended_backtest.run_walkforward',
                        lambda *a, **kw: _result(gate_pass=False, sharpe_delta=-0.5))
    monkeypatch.setattr('psycopg2.connect', _FakeConn(recent=True))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.WARN
    assert 'FAIL' in r['detail']


def test_fail_in_live_returns_fail(monkeypatch):
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    monkeypatch.setattr('backtest.regime_blended_backtest.run_walkforward',
                        lambda *a, **kw: _result(gate_pass=False, sharpe_delta=-0.5))
    monkeypatch.setattr('psycopg2.connect', _FakeConn(recent=True))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.FAIL


def test_stale_snapshot_returns_warn(monkeypatch):
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    monkeypatch.setattr('backtest.regime_blended_backtest.run_walkforward',
                        lambda *a, **kw: _result(gate_pass=True))
    monkeypatch.setattr('psycopg2.connect', _FakeConn(recent=False))
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.WARN
    assert 'stale' in r['detail'].lower()


def test_no_backfill_in_live_returns_fail(monkeypatch):
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    monkeypatch.setattr('backtest.regime_blended_backtest.run_walkforward',
                        lambda *a, **kw: {'error': 'strategy_regime_backtests has no rows — run backfill first'})
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.FAIL
    assert 'no backtests' in r['detail'].lower() or 'no rows' in r['detail'].lower()


def test_no_backfill_in_dry_run_returns_warn(monkeypatch):
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    monkeypatch.setattr('backtest.regime_blended_backtest.run_walkforward',
                        lambda *a, **kw: {'error': 'strategy_regime_backtests has no rows — run backfill first'})
    r = doc.check_regime_blended_gate_b()
    assert r['severity'] == doc.WARN


def test_walk_forward_exception_returns_warn(monkeypatch):
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')

    def raise_(*a, **kw):
        raise RuntimeError('db down')

    monkeypatch.setattr('backtest.regime_blended_backtest.run_walkforward', raise_)
    r = doc.check_regime_blended_gate_b()
    # Walk-forward crash is not a gate fail; warn so operator investigates.
    assert r['severity'] == doc.WARN
    assert 'walk-forward failed' in r['detail']


# ── Fakes ────────────────────────────────────────────────────────────────────

class _FakeConn:
    """Mimics psycopg2.connect for freshness probe; returns recent or stale row."""

    def __init__(self, *, recent):
        self.recent = recent

    def __call__(self, *_args, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return _FakeCursor(recent=self.recent)


class _FakeCursor:

    def __init__(self, *, recent):
        if recent:
            self._row = (datetime.now(timezone.utc) - timedelta(hours=1),)
        else:
            self._row = (datetime.now(timezone.utc) - timedelta(days=20),)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_a, **_kw):
        pass

    def fetchone(self):
        return self._row

    def close(self):
        pass
