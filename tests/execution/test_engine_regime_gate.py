from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest
from datetime import date
from execution.engine import run_strategies


class _FakeStrategy:
    def __init__(self, sid):
        self.id = sid
        self.generate_signals_calls = 0

    def generate_signals(self, prices, regime, universe, aux_data):
        self.generate_signals_calls += 1
        return []


def test_eligible_strategy_runs(monkeypatch):
    s = _FakeStrategy('S1')
    monkeypatch.setattr('execution.engine.is_eligible',
                        lambda sid, regime: sid == 'S1' and regime == 'LOW_VOL')
    result = run_strategies([s], prices=None, regime='LOW_VOL', universe=[], aux_data={})
    assert s.generate_signals_calls == 1
    assert 'S1' in result


def test_ineligible_strategy_skipped(monkeypatch):
    s = _FakeStrategy('S1')
    monkeypatch.setattr('execution.engine.is_eligible', lambda sid, regime: False)
    result = run_strategies([s], prices=None, regime='LOW_VOL', universe=[], aux_data={})
    assert s.generate_signals_calls == 0
    assert 'S1' not in result


def test_gate_receives_string_when_regime_is_dict(monkeypatch):
    """Regression: 2026-05-13 zero-signal cycle. run_strategies was passing
    the full regime dict from load_regime() to is_eligible(), which expects
    a state string. The strict DB-backed resolver then rejected every
    strategy with 'unknown regime_state'."""
    seen = []
    monkeypatch.setattr(
        'execution.engine.is_eligible',
        lambda sid, regime: (seen.append(regime), True)[1],
    )
    s = _FakeStrategy('S1')
    regime_dict = {'state': 'TRANSITIONING', 'vix_level': 17.99, 'active_strategies': ['S1']}
    run_strategies([s], prices=None, regime=regime_dict, universe=[], aux_data={})
    assert seen == ['TRANSITIONING'], f'gate received {seen!r}, expected ["TRANSITIONING"]'
    assert s.generate_signals_calls == 1
