#!/usr/bin/env python3
"""
Unit tests for run_strategies.last_run_stats side-channel.

Tests verify that n_strategies_ok (ran_ok) counts strategies that executed
WITHOUT raising, regardless of signal count — not strategies that emitted ≥1
signal.  This is the fix for the "all-flat successful day → healthy=False"
bug.

No DB connection required.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest


def _get_run_strategies():
    """
    Always return the *current* run_strategies from the module cache.

    test_engine_override.py calls importlib.reload(execution.engine), which
    replaces the module object in sys.modules.  A top-level
    ``from execution.engine import run_strategies`` would capture a stale
    reference that pre-dates the reload.  By importing inside every test
    (or via this helper) we always touch the live function object so that
    attribute reads/writes are consistent.
    """
    import execution.engine as _eng
    return _eng.run_strategies


# ──────────────────────────────────────────────────────────
# Fake strategy helpers
# ──────────────────────────────────────────────────────────

class _FakeSignal:
    """Minimal stand-in for a Signal object."""
    pass


class _StrategyOkWithSignal:
    """Runs fine and emits one signal."""
    def __init__(self, sid):
        self.id = sid

    def generate_signals(self, prices, regime, universe, aux_data):
        return [_FakeSignal()]


class _StrategyOkFlat:
    """Runs fine but emits no signals (all-flat day)."""
    def __init__(self, sid):
        self.id = sid

    def generate_signals(self, prices, regime, universe, aux_data):
        return []


class _StrategyRaises:
    """Raises on every call — simulates a broken strategy."""
    def __init__(self, sid):
        self.id = sid

    def generate_signals(self, prices, regime, universe, aux_data):
        raise RuntimeError("strategy exploded")


# ──────────────────────────────────────────────────────────
# Tests: last_run_stats shape
# ──────────────────────────────────────────────────────────

def test_last_run_stats_mixed(monkeypatch):
    """
    1 raises, 1 flat (ok, no signals), 1 with signal → total=3, errored=1, ran_ok=2.
    This is the spec's explicit test case.
    """
    monkeypatch.setattr('execution.engine.is_eligible', lambda sid, regime: True)
    monkeypatch.setattr('execution.engine.instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr('execution.engine._apply_regime_overrides_to_signals',
                        lambda sid, sigs, regime: None)

    run_strategies = _get_run_strategies()
    s_raises = _StrategyRaises('S_raises')
    s_flat = _StrategyOkFlat('S_flat')
    s_signal = _StrategyOkWithSignal('S_signal')

    result = run_strategies(
        [s_raises, s_flat, s_signal],
        prices=None,
        regime='LOW_VOL',
        universe=[],
        aux_data={},
    )

    stats = run_strategies.last_run_stats
    assert stats == {'total': 3, 'errored': 1, 'ran_ok': 2}, (
        f"Expected total=3, errored=1, ran_ok=2; got {stats}"
    )


def test_last_run_stats_all_flat(monkeypatch):
    """
    All strategies run fine but emit no signals.
    → total=N, errored=0, ran_ok=N.
    This is the all-flat-successful-day canary: ran_ok>0 so healthy=True.
    """
    monkeypatch.setattr('execution.engine.is_eligible', lambda sid, regime: True)
    monkeypatch.setattr('execution.engine.instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr('execution.engine._apply_regime_overrides_to_signals',
                        lambda sid, sigs, regime: None)

    run_strategies = _get_run_strategies()
    strategies = [_StrategyOkFlat(f'S_{i}') for i in range(4)]
    result = run_strategies(
        strategies,
        prices=None,
        regime='LOW_VOL',
        universe=[],
        aux_data={},
    )

    stats = run_strategies.last_run_stats
    assert stats == {'total': 4, 'errored': 0, 'ran_ok': 4}, (
        f"Expected total=4, errored=0, ran_ok=4 (all-flat-healthy); got {stats}"
    )
    # strategy_results shape must be unchanged: exception path AND flat path → []
    for sid, sigs in result.items():
        assert sigs == [], f"{sid} expected [], got {sigs!r}"


def test_last_run_stats_all_errored(monkeypatch):
    """
    All strategies raise.
    → total=N, errored=N, ran_ok=0 → healthy=False (flatten refused).
    """
    monkeypatch.setattr('execution.engine.is_eligible', lambda sid, regime: True)
    monkeypatch.setattr('execution.engine.instrument_class_for', lambda sid: 'equity')

    run_strategies = _get_run_strategies()
    strategies = [_StrategyRaises(f'S_{i}') for i in range(3)]
    result = run_strategies(
        strategies,
        prices=None,
        regime='LOW_VOL',
        universe=[],
        aux_data={},
    )

    stats = run_strategies.last_run_stats
    assert stats == {'total': 3, 'errored': 3, 'ran_ok': 0}, (
        f"Expected total=3, errored=3, ran_ok=0; got {stats}"
    )
    # strategy_results shape must be unchanged even in exception path
    for sid, sigs in result.items():
        assert sigs == [], f"{sid} expected [], got {sigs!r}"


def test_last_run_stats_all_with_signals(monkeypatch):
    """
    All strategies emit ≥1 signal.
    → errored=0, ran_ok==total.
    """
    monkeypatch.setattr('execution.engine.is_eligible', lambda sid, regime: True)
    monkeypatch.setattr('execution.engine.instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr('execution.engine._apply_regime_overrides_to_signals',
                        lambda sid, sigs, regime: None)

    run_strategies = _get_run_strategies()
    strategies = [_StrategyOkWithSignal(f'S_{i}') for i in range(5)]
    run_strategies(
        strategies,
        prices=None,
        regime='LOW_VOL',
        universe=[],
        aux_data={},
    )

    stats = run_strategies.last_run_stats
    assert stats == {'total': 5, 'errored': 0, 'ran_ok': 5}


def test_strategy_results_shape_unchanged_exception_path(monkeypatch):
    """
    Regression guard: exception path must still store [] in results dict,
    NOT None.  write_signals / update_pnl / detect_confluence iterate
    strategy_results.values() with `for sig in signals` — None would break them.
    """
    monkeypatch.setattr('execution.engine.is_eligible', lambda sid, regime: True)
    monkeypatch.setattr('execution.engine.instrument_class_for', lambda sid: 'equity')

    run_strategies = _get_run_strategies()
    s = _StrategyRaises('S_raises')
    result = run_strategies(
        [s],
        prices=None,
        regime='LOW_VOL',
        universe=[],
        aux_data={},
    )

    assert 'S_raises' in result, "errored strategy must still appear in results"
    assert result['S_raises'] == [], (
        "errored strategy must store [] not None — downstream iterators depend on this"
    )


# ──────────────────────────────────────────────────────────
# Tests: all-flat canary for write_eod_health
# ──────────────────────────────────────────────────────────

def test_write_eod_health_all_flat_healthy(monkeypatch):
    """
    All-flat-but-healthy canary (no DB required).

    A run where n_strategies_ok == n_strategies_total > 0 and zero signals
    were emitted must produce healthy=True.  write_eod_health only receives
    n_strategies_ok; signal count is irrelevant to its logic.
    """
    from execution.engine import write_eod_health
    from datetime import date

    captured = {}

    class _FakeCursor:
        def execute(self, sql, params):
            captured['params'] = params

    write_eod_health(
        _FakeCursor(),
        date(2026, 5, 31),
        rc=0,
        n_strategies_ok=5,   # all 5 ran fine (none raised), even though 0 signals emitted
        n_strategies_total=5,
        regime_ok=True,
        universe_size=100,
    )

    # The 7th positional param is `healthy`
    healthy_value = captured['params'][6]
    assert healthy_value is True, (
        f"All-flat-but-healthy run must produce healthy=True, got {healthy_value}"
    )


def test_write_eod_health_all_errored_not_healthy(monkeypatch):
    """
    All strategies errored → ran_ok=0 → healthy=False (no DB required).
    """
    from execution.engine import write_eod_health
    from datetime import date

    captured = {}

    class _FakeCursor:
        def execute(self, sql, params):
            captured['params'] = params

    write_eod_health(
        _FakeCursor(),
        date(2026, 5, 31),
        rc=0,
        n_strategies_ok=0,   # all errored
        n_strategies_total=5,
        regime_ok=True,
        universe_size=100,
    )

    healthy_value = captured['params'][6]
    assert healthy_value is False, (
        f"All-errored run must produce healthy=False, got {healthy_value}"
    )
