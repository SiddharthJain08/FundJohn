"""Tests for SP-2 Phase C doctor + system_check universe-recs checks.

Covers:
  - doctor._check_universe_recs_freshness gate-off → severity='pass'
  - doctor._check_universe_recs_freshness gate-on, no recs → severity='warn'
  - doctor._check_universe_recs_freshness gate-on, fresh recs → severity='pass'
  - doctor._check_universe_recs_freshness gate-on, stale >8d → severity='warn'
  - doctor._check_universe_recs_freshness gate-on, stale >14d → severity='fail'
  - doctor._check_universe_recs_freshness gate-on, DB error → severity='warn'
  - system_check universe_recs_health gate-off → Status.PASS, exit 0
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.maintenance.doctor import _check_universe_recs_freshness


# ── doctor check ──────────────────────────────────────────────────────────


def test_doctor_gate_off_returns_pass(monkeypatch):
    """With OPENCLAW_UNIVERSE_RECS unset, check returns severity='pass' without
    touching the DB (the check function returns a dict, not an enum)."""
    monkeypatch.delenv('OPENCLAW_UNIVERSE_RECS', raising=False)

    # Stub the DB seam to blow up if it's ever called.
    def _boom():
        raise AssertionError('DB query must not run when gate is off')

    monkeypatch.setattr(
        'src.maintenance.doctor._universe_recs_latest_recommended_at', _boom)

    r = _check_universe_recs_freshness()
    assert isinstance(r, dict), f'expected dict, got {type(r)}'
    assert r['severity'] == 'pass', f'expected pass, got {r}'
    assert 'gate off' in r['detail']


def test_doctor_gate_on_no_recs_returns_warn(monkeypatch):
    """Gate on, table empty → warn (timer never run)."""
    monkeypatch.setenv('OPENCLAW_UNIVERSE_RECS', '1')
    monkeypatch.setattr(
        'src.maintenance.doctor._universe_recs_latest_recommended_at',
        lambda: None,
    )
    r = _check_universe_recs_freshness()
    assert r['severity'] == 'warn', f'expected warn, got {r}'


def test_doctor_gate_on_fresh_returns_pass(monkeypatch):
    """Gate on, recommended_at within 8d → pass."""
    monkeypatch.setenv('OPENCLAW_UNIVERSE_RECS', '1')
    fresh = datetime.now(timezone.utc) - timedelta(days=3)
    monkeypatch.setattr(
        'src.maintenance.doctor._universe_recs_latest_recommended_at',
        lambda: fresh,
    )
    r = _check_universe_recs_freshness()
    assert r['severity'] == 'pass', f'expected pass, got {r}'
    assert '3d ago' in r['detail']


def test_doctor_gate_on_stale_8d_returns_warn(monkeypatch):
    """Gate on, recommended_at 10d ago → warn (>8d threshold)."""
    monkeypatch.setenv('OPENCLAW_UNIVERSE_RECS', '1')
    stale = datetime.now(timezone.utc) - timedelta(days=10)
    monkeypatch.setattr(
        'src.maintenance.doctor._universe_recs_latest_recommended_at',
        lambda: stale,
    )
    r = _check_universe_recs_freshness()
    assert r['severity'] == 'warn', f'expected warn, got {r}'


def test_doctor_gate_on_stale_14d_returns_fail(monkeypatch):
    """Gate on, recommended_at 16d ago → fail (>14d threshold)."""
    monkeypatch.setenv('OPENCLAW_UNIVERSE_RECS', '1')
    very_stale = datetime.now(timezone.utc) - timedelta(days=16)
    monkeypatch.setattr(
        'src.maintenance.doctor._universe_recs_latest_recommended_at',
        lambda: very_stale,
    )
    r = _check_universe_recs_freshness()
    assert r['severity'] == 'fail', f'expected fail, got {r}'


def test_doctor_gate_on_exactly_14d_returns_warn(monkeypatch):
    """Gate on, recommended_at exactly 14d ago → warn.

    The check uses strictly-greater-than 14d for fail, so 14d is still warn.
    This locks the boundary so the threshold cannot drift silently.
    """
    monkeypatch.setenv('OPENCLAW_UNIVERSE_RECS', '1')
    exactly_14d = datetime.now(timezone.utc) - timedelta(days=14)
    monkeypatch.setattr(
        'src.maintenance.doctor._universe_recs_latest_recommended_at',
        lambda: exactly_14d,
    )
    r = _check_universe_recs_freshness()
    assert r['severity'] == 'warn', (
        f'expected warn at exactly 14d boundary, got {r}'
    )


def test_doctor_gate_on_db_error_returns_warn(monkeypatch):
    """Gate on, DB query raises → warn (never propagates)."""
    monkeypatch.setenv('OPENCLAW_UNIVERSE_RECS', '1')
    monkeypatch.setattr(
        'src.maintenance.doctor._universe_recs_latest_recommended_at',
        lambda: (_ for _ in ()).throw(RuntimeError('connection refused')),
    )
    r = _check_universe_recs_freshness()
    assert r['severity'] == 'warn', f'expected warn on DB error, got {r}'
    assert 'query failed' in r['detail']


# ── system_check (gate-off) ────────────────────────────────────────────────


def test_system_check_gate_off_exits_zero(monkeypatch):
    """With OPENCLAW_UNIVERSE_RECS unset, system_check exits 0 (PASS).
    Uses in-process API to avoid CLI flag drift."""
    monkeypatch.delenv('OPENCLAW_UNIVERSE_RECS', raising=False)

    # Ensure checks are registered.
    import src.system_checks.checks  # noqa: F401 (side-effect import)
    from system_checks import run_one
    from system_checks.types import Status

    result = run_one('universe_recs_health')
    # When POSTGRES_URI is set the gate-off branch runs and returns PASS.
    # When POSTGRES_URI is unset the runner returns SKIP before the function
    # body executes (requires=['db']).  Either way must never be WARN/FAIL.
    assert result.status in (Status.PASS, Status.SKIP), (
        f'expected PASS or SKIP, got {result}'
    )
    if result.status is Status.PASS:
        assert 'gate off' in result.detail


def test_system_check_cli_gate_off_exits_zero():
    """Subprocess smoke: python3 -m src.system_checks --check universe_recs_health
    with gate off exits 0."""
    env = {'OPENCLAW_UNIVERSE_RECS': ''}  # ensure gate is off
    import os
    full_env = {**os.environ, **env}
    full_env.pop('OPENCLAW_UNIVERSE_RECS', None)  # fully remove

    proc = subprocess.run(
        [sys.executable, '-m', 'src.system_checks',
         '--check', 'universe_recs_health', '--json'],
        capture_output=True, text=True,
        env=full_env,
        # cwd must be the repo root (was a long-dead authoring-worktree path)
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert proc.returncode == 0, (
        f'expected exit 0, got {proc.returncode}\n'
        f'stdout={proc.stdout}\nstderr={proc.stderr}'
    )
