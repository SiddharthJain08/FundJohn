"""TDD tests for src/research/exit_timing/passive_window_feasibility.py.

All frames are synthetic in-memory.  The real cache is NEVER read here.
Run:  PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_passive_window_feasibility.py -q
"""
from __future__ import annotations

import math
import pandas as pd
import pytest

from research.exit_timing.passive_window_feasibility import (
    HS_CLOSE,
    MIN_SESSIONS,
    close_benchmark,
    clustered_t,
    event_improvements,
    verdict,
)

# ── test helpers ──────────────────────────────────────────────────────────

def _make_tdf(
    minutes: list[int],
    price: float = 100.0,
    overrides: dict | None = None,
) -> pd.DataFrame:
    """Flat world: h=l=o=c=vw=price, v=1000 at every given minute."""
    rows = []
    for m in minutes:
        rows.append({
            "minute": m,
            "o": price, "h": price, "l": price, "c": price,
            "v": 1000.0, "vw": price,
        })
    df = pd.DataFrame(rows)
    if overrides:
        for m, cols in overrides.items():
            for col, val in cols.items():
                df.loc[df["minute"] == m, col] = val
    return df


def _full_minutes(price: float = 100.0) -> list[int]:
    """All minutes 0..389 — covers the full session."""
    return list(range(390))


# ── Test 1: FLAT world — sign checks ─────────────────────────────────────

def test_flat_world_signs():
    """Flat world (vw=h=l=close=100 everywhere):
    - sell never fills in morning (h=100 < ask=100*(1+hs)) → sell_naive=0.
    - buy marketable pays hs → buy_mkt_naive ≈ -(hs - hs_c) < 0.
    - buy passive bid never hit (l=100 > bid=100*(1-hs)) → forced 10am → pays hs.
    """
    hs = 5.0
    tdf = _make_tdf(_full_minutes(), price=100.0)
    res = event_improvements(tdf, hs, HS_CLOSE)
    assert res is not None

    # sell: ask_m = 100*(1+5/1e4) = 100.05 > max(h)=100 → no morning fill
    assert res["sell_fill_morning"] is False
    # afternoon ask_pm similarly > 100 → no afternoon fill
    assert res["sell_fill_afternoon"] is False
    # forced MOC = baseline → zero improvement
    assert res["sell_naive"] == pytest.approx(0.0, abs=1e-9)

    # buy marketable: pays morning vwap (100)*(1+hs/1e4); baseline=100*(1+hs_c/1e4)
    # improvement = (baseline - cost)/close*1e4 = -(hs - hs_c) < 0
    assert res["buy_mkt_naive"] < 0
    assert res["buy_mkt_naive"] == pytest.approx(-(hs - HS_CLOSE), rel=1e-6)

    # buy passive: bid=100*(1-hs/1e4)<100, l=100>bid → no fill → forced at vw30*(1+hs/1e4)
    assert res["buy_pass_fill"] is False
    assert res["buy_pass_naive"] < 0


# ── Test 2: SELL morning fill ─────────────────────────────────────────────

def test_sell_morning_fill():
    """A spike at minute 7 above ask triggers morning fill → sell_naive > 0."""
    hs = 5.0
    price = 100.0
    spike = price * (1 + hs / 1e4) * 1.01  # well above ask_m
    tdf = _make_tdf(_full_minutes(), price=price, overrides={7: {"h": spike}})
    res = event_improvements(tdf, hs, HS_CLOSE)
    assert res is not None
    assert res["sell_fill_morning"] is True
    assert res["sell_fill_afternoon"] is False
    # fill at ask_m = mid5*(1+hs/1e4) = 100*(1.0005) = 100.05
    # baseline_sell = 100*(1-0.0001) = 99.99
    # improvement = (100.05 - 99.99)/100 * 1e4 > 0
    assert res["sell_naive"] > 0


# ── Test 3: SELL afternoon fill ───────────────────────────────────────────

def test_sell_afternoon_fill():
    """No morning spike but a spike at minute 370 triggers afternoon fill."""
    hs = 5.0
    price = 100.0
    spike = price * (1 + hs / 1e4) * 1.01
    tdf = _make_tdf(_full_minutes(), price=price, overrides={370: {"h": spike}})
    res = event_improvements(tdf, hs, HS_CLOSE)
    assert res is not None
    assert res["sell_fill_morning"] is False
    assert res["sell_fill_afternoon"] is True
    assert res["sell_naive"] > 0


# ── Test 4: BUY passive fill ──────────────────────────────────────────────

def test_buy_passive_fill():
    """A low dip at minute 20 below bid → passive fill → buy_pass_naive > 0."""
    hs = 5.0
    price = 100.0
    bid = price * (1 - hs / 1e4)
    dip = bid * 0.999  # slightly below bid
    tdf = _make_tdf(_full_minutes(), price=price, overrides={20: {"l": dip}})
    res = event_improvements(tdf, hs, HS_CLOSE)
    assert res is not None
    assert res["buy_pass_fill"] is True
    # cost = bid = 99.995, baseline = 100*(1+0.0001) = 100.01
    # improvement = (100.01 - 99.995)/100 * 1e4 > 0
    assert res["buy_pass_naive"] > 0


# ── Test 5: close_benchmark vol-weighting ─────────────────────────────────

def test_close_benchmark_vol_weighting():
    """Two-minute dump with different volumes: vw is correctly vol-weighted."""
    rows = [
        {"minute": 386, "o": 100.0, "h": 101.0, "l": 99.0,
         "c": 100.0, "v": 1000.0, "vw": 100.0},
        {"minute": 387, "o": 102.0, "h": 103.0, "l": 101.0,
         "c": 102.0, "v": 3000.0, "vw": 102.0},
    ]
    tdf = pd.DataFrame(rows)
    result = close_benchmark(tdf)
    # expected: (100*1000 + 102*3000) / (1000+3000) = 406000/4000 = 101.5
    assert result == pytest.approx(101.5, rel=1e-9)


# ── Test 6: event_improvements returns None when close or anchor missing ─

def test_event_none_when_close_missing():
    """If no minute >= DUMP_MIN, close is None → event_improvements returns None."""
    tdf = _make_tdf(list(range(380)))  # minutes 0..379, no dump window
    res = event_improvements(tdf, 5.0, HS_CLOSE)
    assert res is None


def test_event_none_when_minute5_missing():
    """If minute 5 is absent, event_improvements returns None."""
    minutes = [m for m in _full_minutes() if m != 5]
    tdf = _make_tdf(minutes)
    res = event_improvements(tdf, 5.0, HS_CLOSE)
    assert res is None


def test_event_none_when_minute15_missing():
    """If minute 15 is absent, event_improvements returns None."""
    minutes = [m for m in _full_minutes() if m != 15]
    tdf = _make_tdf(minutes)
    res = event_improvements(tdf, 5.0, HS_CLOSE)
    assert res is None


# ── Test 7: clustered_t known values ─────────────────────────────────────

def test_clustered_t_known_values():
    """Verify clustered_t arithmetic against hand calculation.

    Two sessions: s1 mean=2.0, s2 mean=4.0 → cross-session mean=3.0.
    SD_ddof1 = sqrt(((2-3)^2+(4-3)^2)/1) = sqrt(2).
    SE = sqrt(2)/sqrt(2) = 1.0 → t = 3.0/1.0 = 3.0.
    """
    rows = [
        {"session": "2024-01-01", "val": 1.0},
        {"session": "2024-01-01", "val": 3.0},  # s1 mean = 2.0
        {"session": "2024-01-02", "val": 4.0},  # s2 mean = 4.0
    ]
    mean, t, n = clustered_t(rows, "val")
    assert n == 2
    assert mean == pytest.approx(3.0, rel=1e-9)
    assert t == pytest.approx(3.0, rel=1e-6)


def test_clustered_t_single_session_nan_t():
    """Single session → t is NaN."""
    rows = [{"session": "2024-01-01", "val": 5.0}]
    mean, t, n = clustered_t(rows, "val")
    assert n == 1
    assert math.isnan(t)


def test_clustered_t_empty_rows():
    """Empty rows → all NaN / 0."""
    mean, t, n = clustered_t([], "val")
    assert n == 0
    assert math.isnan(mean)
    assert math.isnan(t)


# ── Test 8: verdict branching ─────────────────────────────────────────────

def test_verdict_invalid_data():
    """n_sessions < MIN_SESSIONS → INVALID-DATA."""
    # stats tuples: (mean, t, n)
    v = verdict(
        combined_hybrid_stats=(1.0, 2.0, 400),
        combined_allpassive_stats=(1.0, 2.0, 400),
        sell_naive_stats=(1.0, 2.0, 400),
        n_sessions=MIN_SESSIONS - 1,
    )
    assert v == "INVALID-DATA"


def test_verdict_park_all_negative():
    """All three signals <= 0 → PARK (buy drag dominates)."""
    v = verdict(
        combined_hybrid_stats=(-1.0, -2.0, 700),
        combined_allpassive_stats=(-0.5, -1.0, 700),
        sell_naive_stats=(-0.1, -0.5, 700),
        n_sessions=700,
    )
    assert v == "PARK"


def test_verdict_inconclusive_when_allpassive_positive():
    """all_passive > 0 → INCONCLUSIVE-LEAN-SELL (even when hybrid <= 0)."""
    v = verdict(
        combined_hybrid_stats=(-1.0, -2.0, 700),   # hybrid negative
        combined_allpassive_stats=(0.5, 1.0, 700),  # all-passive positive
        sell_naive_stats=(-0.1, -0.5, 700),
        n_sessions=700,
    )
    assert v == "INCONCLUSIVE-LEAN-SELL"


def test_verdict_inconclusive_when_sell_positive():
    """sell_naive > 0 → INCONCLUSIVE-LEAN-SELL (even when hybrid <= 0)."""
    v = verdict(
        combined_hybrid_stats=(-1.0, -2.0, 700),
        combined_allpassive_stats=(-0.5, -1.0, 700),
        sell_naive_stats=(0.3, 0.8, 700),           # sell positive
        n_sessions=700,
    )
    assert v == "INCONCLUSIVE-LEAN-SELL"


def test_verdict_hybrid_positive_is_inconclusive():
    """hybrid > 0 also → INCONCLUSIVE-LEAN-SELL (sell + buy combined win)."""
    v = verdict(
        combined_hybrid_stats=(0.5, 1.0, 700),     # hybrid positive
        combined_allpassive_stats=(0.8, 1.5, 700),
        sell_naive_stats=(2.0, 3.0, 700),
        n_sessions=700,
    )
    assert v == "INCONCLUSIVE-LEAN-SELL"


# ── Test 9: oracle sell always >= naive sell ──────────────────────────────

def test_oracle_sell_at_least_naive():
    """sell_oracle >= sell_naive (oracle is an upper bound)."""
    hs = 5.0
    price = 100.0
    spike = price * (1 + hs / 1e4) * 1.05
    tdf = _make_tdf(_full_minutes(), price=price, overrides={7: {"h": spike}})
    res = event_improvements(tdf, hs, HS_CLOSE)
    assert res is not None
    assert res["sell_oracle"] >= res["sell_naive"]


# ── Test 10: no afternoon minutes — sell_oracle falls back to morning only ─

def test_sell_oracle_without_afternoon_minutes():
    """If no minutes in 360-389, oracle uses morning high only (no crash)."""
    minutes = [m for m in _full_minutes() if m < 360]  # early close
    price = 100.0
    tdf = _make_tdf(minutes, price=price)
    res = event_improvements(tdf, 5.0, HS_CLOSE)
    # close_benchmark requires minutes >= 385, so we need to inject a dump bar
    # manually; early close means None close → return None. Adjust: keep minute 385.
    minutes_with_dump = [m for m in _full_minutes() if m < 360 or m == 385]
    tdf2 = _make_tdf(minutes_with_dump, price=price)
    res2 = event_improvements(tdf2, 5.0, HS_CLOSE)
    assert res2 is not None  # must not raise; no afternoon minutes is fine
    # no afternoon data → sell_oracle = morning-only
    assert res2["sell_oracle"] is not None
