"""Tests for regime_gate.is_eligible after DB switch. Module mocks the
resolver so we exercise gate-logic decisions, not the DB itself."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import regime_gate  # noqa: E402


def test_eligible_when_resolver_returns_true(monkeypatch):
    monkeypatch.setattr(regime_gate, '_resolver_is_eligible',
                        lambda sid, r: True)
    assert regime_gate.is_eligible('s1', 'LOW_VOL') is True


def test_not_eligible_when_resolver_returns_false(monkeypatch):
    monkeypatch.setattr(regime_gate, '_resolver_is_eligible',
                        lambda sid, r: False)
    assert regime_gate.is_eligible('s1', 'HIGH_VOL') is False


def test_unknown_regime_state_rejected(monkeypatch):
    """Gate semantics: bogus regime string → False regardless of resolver."""
    monkeypatch.setattr(regime_gate, '_resolver_is_eligible',
                        lambda sid, r: True)
    assert regime_gate.is_eligible('s1', 'NOT_A_REGIME') is False


def test_resolver_unavailable_falls_back_to_true(monkeypatch):
    """If the resolver raises (DB down), gate fails-open (returns True).
    The doctor check + cycle preflight catch the underlying issue."""
    def boom(sid, r): raise RuntimeError('db down')
    monkeypatch.setattr(regime_gate, '_resolver_is_eligible', boom)
    assert regime_gate.is_eligible('s1', 'LOW_VOL') is True
