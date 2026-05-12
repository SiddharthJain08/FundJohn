from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
