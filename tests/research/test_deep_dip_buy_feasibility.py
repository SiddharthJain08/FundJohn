"""Tests for SP-6 deep-dip buy-leg feasibility (pure-function core).

Prereg: docs/superpowers/specs/2026-06-08-sp6-deep-dip-buy-feasibility-prereg.md
"""
import math
import pathlib
import sys

import pandas as pd
import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from research.exit_timing.deep_dip_buy_feasibility import (  # noqa: E402
    HS_C,
    MIN_SESSIONS,
    _fill_rate,
    _touch_fraction,
    clustered_t,
    event_deep_dip,
    verdict,
)

COLS = ["minute", "o", "h", "l", "c", "v", "vw"]


def make_session(rows):
    """rows: list of (minute, o, h, l, c, v, vw) tuples -> DataFrame."""
    return pd.DataFrame(rows, columns=COLS)


def base_rows(open0=100.0, fill_low=None, pm_vw=100.0, close_vw=100.0):
    """A minimal valid session: open at minute 0, a fill-window low at minute 5,
    a PM bar at minute 300, a close (dump) bar at minute 390."""
    if fill_low is None:
        fill_low = open0  # never dips
    return [
        (0, open0, open0, open0, open0, 1000, open0),        # open
        (5, open0, open0, fill_low, open0, 1000, open0),     # fill-window low
        (300, pm_vw, pm_vw, pm_vw, pm_vw, 1000, pm_vw),      # PM fallback
        (390, close_vw, close_vw, close_vw, close_vw, 1000, close_vw),  # dump
    ]


# ── event_deep_dip ──────────────────────────────────────────────────────────

def test_clear_through_fill_flat_close():
    """Dip 50bps below open, close ~= open -> ~+50bps improvement, clear-through."""
    # limit = 100*(1-0.005) = 99.5; low 99.0 is clearly through.
    tdf = make_session(base_rows(open0=100.0, fill_low=99.0, pm_vw=100.0, close_vw=100.0))
    ev = event_deep_dip(tdf, dip_bps=50.0, hs_c=HS_C)
    assert ev is not None
    assert ev["filled"] is True
    assert ev["touch_boundary"] is False  # 99.0 well below 99.48 band
    # improvement ~= (100.01 - 99.5)/100*1e4 = 51 bps
    assert ev["improvement"] == pytest.approx(51.0, abs=0.2)


def test_no_fill_falls_to_pm():
    """Low never reaches the limit -> PM vwap fallback; PM~=close -> ~0."""
    tdf = make_session(base_rows(open0=100.0, fill_low=99.6, pm_vw=100.0, close_vw=100.0))
    ev = event_deep_dip(tdf, dip_bps=50.0, hs_c=HS_C)
    assert ev is not None
    assert ev["filled"] is False
    assert ev["touch_boundary"] is None
    assert ev["improvement"] == pytest.approx(0.0, abs=0.2)


def test_touch_boundary_flagged():
    """Low grazes the limit within TOUCH_BPS -> filled but over-fill-ambiguous."""
    # limit = 99.5; band lower edge = 99.5*(1-2bps) = 99.48006; low = 99.49 in band.
    tdf = make_session(base_rows(open0=100.0, fill_low=99.49, pm_vw=100.0, close_vw=100.0))
    ev = event_deep_dip(tdf, dip_bps=50.0, hs_c=HS_C)
    assert ev is not None
    assert ev["filled"] is True
    assert ev["touch_boundary"] is True


def test_continuation_close_below_limit_is_negative():
    """Bought the dip at 99.5 but the name kept falling to 99.0 -> negative."""
    tdf = make_session(base_rows(open0=100.0, fill_low=99.0, pm_vw=99.0, close_vw=99.0))
    ev = event_deep_dip(tdf, dip_bps=50.0, hs_c=HS_C)
    assert ev is not None
    assert ev["filled"] is True
    assert ev["improvement"] < 0  # close baseline 99.0 < fill 99.5


def test_open_to_close_diagnostic():
    """open_to_close is the unconditional drift, independent of the fill."""
    tdf = make_session(base_rows(open0=100.0, fill_low=99.6, pm_vw=101.0, close_vw=101.0))
    ev = event_deep_dip(tdf, dip_bps=50.0, hs_c=HS_C)
    # (101 - 100)/101*1e4 ~= +99 bps
    assert ev["open_to_close"] == pytest.approx(99.0, abs=0.5)


def test_void_missing_open():
    rows = base_rows()
    rows = [r for r in rows if r[0] != 0]  # drop minute 0
    assert event_deep_dip(make_session(rows), 50.0, HS_C) is None


def test_void_missing_close_benchmark():
    rows = base_rows()
    rows = [r for r in rows if r[0] < 385]  # drop dump bar
    assert event_deep_dip(make_session(rows), 50.0, HS_C) is None


def test_void_missing_pm_window():
    rows = base_rows()
    rows = [r for r in rows if not (240 <= r[0] <= 330)]  # drop PM bar
    assert event_deep_dip(make_session(rows), 50.0, HS_C) is None


def test_dip_level_changes_fill():
    """A 99.6 low fills a 25bps limit (99.75) but not a 50bps limit (99.5)."""
    tdf = make_session(base_rows(open0=100.0, fill_low=99.6, pm_vw=100.0, close_vw=100.0))
    ev25 = event_deep_dip(tdf, dip_bps=25.0, hs_c=HS_C)
    ev50 = event_deep_dip(tdf, dip_bps=50.0, hs_c=HS_C)
    assert ev25["filled"] is True
    assert ev50["filled"] is False


# ── aggregation helpers ───────────────────────────────────────────────────

def test_fill_rate_and_touch_fraction():
    rows = [
        {"filled": True, "touch_boundary": True},
        {"filled": True, "touch_boundary": False},
        {"filled": False, "touch_boundary": None},
        {"filled": False, "touch_boundary": None},
    ]
    assert _fill_rate(rows) == pytest.approx(0.5)
    # touch fraction is over FILLS only: 1 of 2
    assert _touch_fraction(rows) == pytest.approx(0.5)


def test_clustered_t_reused():
    rows = [
        {"session": "d1", "improvement": 10.0},
        {"session": "d1", "improvement": 20.0},
        {"session": "d2", "improvement": 30.0},
    ]
    mean, t, n = clustered_t(rows, "improvement")
    # session means: d1=15, d2=30 -> mean 22.5, n=2
    assert mean == pytest.approx(22.5)
    assert n == 2


# ── verdict ─────────────────────────────────────────────────────────────────

def test_verdict_invalid_data():
    assert verdict((5.0, 4.0, 100), 0.1, n_sessions=100) == "INVALID-DATA"


def test_verdict_kill_nonpositive():
    assert verdict((-2.0, -3.0, 800), 0.1, n_sessions=800) == "KILL"
    assert verdict((0.0, 0.0, 800), 0.1, n_sessions=800) == "KILL"


def test_verdict_measured_positive():
    assert verdict((6.0, 4.5, 800), 0.2, n_sessions=800) == "MEASURED-POSITIVE"


def test_verdict_marginal_low_t():
    assert verdict((6.0, 1.5, 800), 0.2, n_sessions=800) == "MARGINAL"


def test_verdict_marginal_high_touch():
    """Positive and significant but fills are over-fill-tainted -> MARGINAL."""
    assert verdict((6.0, 4.5, 800), 0.7, n_sessions=800) == "MARGINAL"


def test_verdict_nan_mean_invalid():
    assert verdict((float("nan"), float("nan"), 800), 0.2, n_sessions=800) == "INVALID-DATA"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
