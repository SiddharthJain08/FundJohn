"""SP-1: tests for filter_valid_greeks defensive layer."""
from datetime import date, timedelta
from types import SimpleNamespace
import pytest

from src.strategies.implementations._greeks_filter import (
    filter_valid_greeks,
    _is_zero_greeks,
)


def _snap(*, delta=0.5, gamma=0.01, theta=-0.1, vega=0.5, rho=0.1,
          dte_days=30, volume=1000, contract_symbol='SPY260618C00742000'):
    return SimpleNamespace(
        contract_symbol=contract_symbol,
        expiry=date.today() + timedelta(days=dte_days),
        volume=volume,
        greeks=SimpleNamespace(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho),
    )


def test_accepts_atm_30dte():
    assert filter_valid_greeks(_snap()) is not None


def test_rejects_0dte_expired_silently():
    snap = _snap(delta=0, gamma=0, theta=0, vega=0, rho=0, dte_days=0)
    assert filter_valid_greeks(snap, alert=False) is None


def test_rejects_zero_volume_silently():
    snap = _snap(delta=0, gamma=0, theta=0, vega=0, rho=0, volume=0)
    assert filter_valid_greeks(snap, alert=False) is None


def test_rejects_and_flags_meaningful_zero_greeks(monkeypatch):
    """When a meaningful contract returns zero greeks, filter rejects AND alerts."""
    alerted = []
    monkeypatch.setattr(
        'src.strategies.implementations._greeks_filter._alert_zero_greeks_anomaly',
        lambda snap: alerted.append(snap),
    )
    snap = _snap(delta=0, gamma=0, theta=0, vega=0, rho=0, volume=500, dte_days=30)
    result = filter_valid_greeks(snap, alert=True)
    assert result is None
    assert len(alerted) == 1


def test_allow_zero_greeks_bypasses_filter():
    snap = _snap(delta=0, dte_days=30, volume=500)
    assert filter_valid_greeks(snap, allow_zero_greeks=True) is not None


def test_is_zero_greeks_helper():
    assert _is_zero_greeks(_snap(delta=0, gamma=0, theta=0, vega=0, rho=0))
    assert not _is_zero_greeks(_snap())


def test_accepts_string_expiry():
    """SP-1: Alpaca CLI gives string expiry like '2026-06-18'; filter must handle."""
    snap = _snap()
    snap.expiry = (date.today() + timedelta(days=30)).isoformat()  # string form
    # Force zero greeks so we exercise the zero-greeks branch (which is where DTE is computed)
    snap.greeks = SimpleNamespace(delta=0, gamma=0, theta=0, vega=0, rho=0)
    # volume=1000 by default → meaningful contract → would alert; suppress for test cleanliness
    result = filter_valid_greeks(snap, alert=False)
    # With string expiry parsed to date, DTE=30 → meaningful zero-greek → returns None
    assert result is None  # confirms parse succeeded (didn't TypeError)


def test_rejects_string_expiry_0dte():
    """SP-1: string expiry for 0-DTE/expired contract correctly silently dropped."""
    snap = _snap(delta=0, gamma=0, theta=0, vega=0, rho=0)
    snap.expiry = date.today().isoformat()  # today = 0-DTE as string
    assert filter_valid_greeks(snap, alert=False) is None
