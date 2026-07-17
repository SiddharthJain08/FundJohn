"""run_backtest bakes the STRATEGY-CONFIGURED max_hold into every backtest
(2026-07-14 operator directive): max_hold_days=None resolves from
strategy_regime_params via the live resolver; explicit ints still pin it
(coupling candidate probes)."""
import pytest

from backtest import unified_backtest as ub
from execution import regime_param_resolver as rpr


def test_configured_max_hold_is_max_of_per_regime_values(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
    vals = {'LOW_VOL': 13, 'TRANSITIONING': None, 'HIGH_VOL': 34, 'CRISIS': None}
    monkeypatch.setattr(rpr, 'max_hold_days_override', lambda sid, r: vals[r])
    assert ub._configured_max_hold_days('S_x') == 34


def test_configured_max_hold_default_when_unset(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
    monkeypatch.setattr(rpr, 'max_hold_days_override', lambda sid, r: None)
    assert ub._configured_max_hold_days('S_x') == ub.DEFAULT_MAX_HOLD_DAYS


def test_configured_max_hold_gate_off_is_legacy_default(monkeypatch):
    # Gate OFF → byte-identical legacy horizon, no DB touch (mirrors the
    # stop/target override gating).
    monkeypatch.delenv('OPENCLAW_BACKTEST_COUPLED_RECS', raising=False)
    def _boom(sid, r):
        raise AssertionError('resolver must not be consulted with gate OFF')
    monkeypatch.setattr(rpr, 'max_hold_days_override', _boom)
    assert ub._configured_max_hold_days('S_x') == ub.DEFAULT_MAX_HOLD_DAYS


def test_configured_max_hold_lookup_failure_falls_back(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
    def _boom(sid, r):
        raise RuntimeError('db down')
    monkeypatch.setattr(rpr, 'max_hold_days_override', _boom)
    assert ub._configured_max_hold_days('S_x') == ub.DEFAULT_MAX_HOLD_DAYS
