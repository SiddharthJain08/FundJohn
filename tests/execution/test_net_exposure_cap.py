"""Book-level net-exposure cap (fix 8, 2026-07-27): |net| bounded at
max_net_frac × gross by de-levering the heavy side — the June 2026
1.6x-net-long lesson."""
import importlib

import pytest

rbs = importlib.import_module("execution.regime_blended_sizer")


@pytest.fixture(autouse=True)
def _cap_enabled(monkeypatch):
    # conftest defaults the cap OFF for e2e harnesses; this file tests it.
    monkeypatch.setenv("OPENCLAW_NET_EXPOSURE_CAP", "1")


def _cap(target, frac=0.6):
    return rbs._apply_net_exposure_cap(dict(target), max_net_frac=frac)


def _net(t):
    return sum(t.values())


def _gross(t):
    return sum(abs(v) for v in t.values())


def test_balanced_book_untouched():
    t = {"A": 5000.0, "B": -5000.0}
    assert _cap(t) == t


def test_within_cap_untouched():
    t = {"A": 7000.0, "B": -3000.0}          # net 4k, gross 10k, cap 6k
    assert _cap(t) == t


def test_long_heavy_book_delevered_to_cap():
    # Cap is against INTENDED gross (λ·NAV): net' == frac × original gross.
    t = {"A": 9000.0, "B": 9000.0, "C": -2000.0}   # net 16k > 0.6×20k
    out = _cap(t)
    assert _net(out) == pytest.approx(0.6 * _gross(t), rel=1e-6)
    assert out["C"] == -2000.0                     # light side untouched
    assert out["A"] == out["B"] and out["A"] < 9000.0


def test_short_heavy_book_delevered_to_cap():
    t = {"A": 2000.0, "B": -9000.0, "C": -9000.0}
    out = _cap(t)
    assert -_net(out) == pytest.approx(0.6 * _gross(t), rel=1e-6)
    assert out["A"] == 2000.0


def test_all_long_book_delevers_to_frac():
    t = {"A": 5000.0, "B": 5000.0}
    out = _cap(t)
    assert out["A"] == pytest.approx(3000.0)       # ×0.6, the policy
    assert out["B"] == pytest.approx(3000.0)


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("OPENCLAW_NET_EXPOSURE_CAP", "0")
    t = {"A": 9000.0, "C": -1000.0}
    assert rbs._apply_net_exposure_cap(dict(t), max_net_frac=0.6) == t


def test_empty_book():
    assert _cap({}) == {}
