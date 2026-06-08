# tests/test_open_spread_study.py
"""TDD tests for src/research/exit_timing/open_spread_study.py.

All tests use synthetic data. NO real API calls (CLI is mocked at the
subprocess level in any runner-touching tests).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from research.exit_timing import open_spread_study as m


# ── half_spread_bps ───────────────────────────────────────────────────────────

def test_half_spread_bps_normal():
    # ap=10.02, bp=9.98 => spread=0.04, mid=10.00, half_spread=0.02/10.00*1e4=20 bps
    result = m.half_spread_bps(10.02, 9.98)
    assert result is not None
    assert abs(result - 20.0) < 1e-9


def test_half_spread_bps_crossed_returns_none():
    # ap <= bp -> None
    assert m.half_spread_bps(9.98, 10.02) is None


def test_half_spread_bps_locked_returns_none():
    # ap == bp -> None
    assert m.half_spread_bps(100.0, 100.0) is None


def test_half_spread_bps_zero_ap_returns_none():
    assert m.half_spread_bps(0.0, 5.0) is None


def test_half_spread_bps_negative_bp_returns_none():
    assert m.half_spread_bps(5.0, -1.0) is None


def test_half_spread_bps_small_spread():
    # 1 cent spread on a $200 stock => 0.5 bps
    result = m.half_spread_bps(200.005, 199.995)
    assert result is not None
    # half = 0.005, mid = 200.0, half_spread_bps = 0.005/200.0 * 1e4 = 0.25 bps
    assert abs(result - 0.25) < 1e-6


# ── window_median_half_spread ─────────────────────────────────────────────────

def test_window_median_fewer_than_3_valid_returns_none():
    # 2 valid quotes -> None
    quotes = [
        {"ap": 10.02, "bp": 9.98},
        {"ap": 10.04, "bp": 9.96},
    ]
    assert m.window_median_half_spread(quotes) is None


def test_window_median_exactly_3_valid():
    # exactly 3 valid => returns median
    quotes = [
        {"ap": 10.02, "bp": 9.98},  # 20.0 bps
        {"ap": 10.04, "bp": 9.96},  # 40.0 bps
        {"ap": 10.06, "bp": 9.94},  # 60.0 bps
    ]
    result = m.window_median_half_spread(quotes)
    assert result is not None
    assert abs(result - 40.0) < 1e-6


def test_window_median_mixed_valid_invalid():
    # crossed and zero quotes are dropped; >=3 valid remain
    quotes = [
        {"ap": 10.02, "bp": 9.98},   # valid: 20 bps
        {"ap": 9.98, "bp": 10.02},   # crossed -> invalid
        {"ap": 0.0, "bp": 0.0},      # zero -> invalid
        {"ap": 10.04, "bp": 9.96},   # valid: 40 bps
        {"ap": 10.06, "bp": 9.94},   # valid: 60 bps
    ]
    result = m.window_median_half_spread(quotes)
    assert result is not None
    assert abs(result - 40.0) < 1e-6


def test_window_median_all_invalid_returns_none():
    quotes = [
        {"ap": 0.0, "bp": 5.0},
        {"ap": 5.0, "bp": 5.0},  # locked
        {"ap": 4.0, "bp": 5.0},  # crossed
    ]
    assert m.window_median_half_spread(quotes) is None


# ── deterministic_sample ──────────────────────────────────────────────────────

def test_deterministic_sample_len_le_n_returns_all():
    pairs = [("AAPL", "2025-01-01"), ("MSFT", "2025-01-02")]
    result = m.deterministic_sample(pairs, 5)
    assert result == sorted(pairs)


def test_deterministic_sample_stride_picks_evenly():
    # 10 pairs, n=3 => step=ceil(10/3)=4 => indices 0,4,8
    pairs = [(f"T{i:02d}", "2025-01-01") for i in range(10)]
    result = m.deterministic_sample(pairs, 3)
    expected = [pairs[0], pairs[4], pairs[8]]
    assert result == expected


def test_deterministic_sample_reproducible():
    pairs = [(f"T{i:03d}", "2025-06-01") for i in range(100)]
    r1 = m.deterministic_sample(pairs, 20)
    r2 = m.deterministic_sample(pairs, 20)
    assert r1 == r2


def test_deterministic_sample_sorts_input():
    # Input unsorted — output should be sorted-then-strided
    pairs = [("ZZZ", "2025-01-01"), ("AAA", "2025-01-01"), ("MMM", "2025-01-01")]
    result = m.deterministic_sample(pairs, 2)
    # sorted: AAA, MMM, ZZZ; n=2, step=ceil(3/2)=2 => indices 0,2 => AAA, ZZZ
    assert result == [("AAA", "2025-01-01"), ("ZZZ", "2025-01-01")]


# ── et_window_utc ─────────────────────────────────────────────────────────────

def test_et_window_utc_edt_summer():
    # 2025-07-01 is EDT (UTC-4): 09:31 ET -> 13:31 UTC
    start, end = m.et_window_utc("2025-07-01", (9, 31, 0), (9, 32, 0))
    assert start == "2025-07-01T13:31:00Z"
    assert end == "2025-07-01T13:32:00Z"


def test_et_window_utc_est_winter():
    # 2026-01-05 is EST (UTC-5): 09:31 ET -> 14:31 UTC
    start, end = m.et_window_utc("2026-01-05", (9, 31, 0), (9, 32, 0))
    assert start == "2026-01-05T14:31:00Z"
    assert end == "2026-01-05T14:32:00Z"


def test_et_window_utc_close_window_edt():
    # Close window 15:55-15:56 ET on an EDT day
    start, end = m.et_window_utc("2025-09-15", (15, 55, 0), (15, 56, 0))
    assert start == "2025-09-15T19:55:00Z"
    assert end == "2025-09-15T19:56:00Z"


def test_et_window_utc_close_window_est():
    # Close window 15:55-15:56 ET on an EST day
    start, end = m.et_window_utc("2025-12-15", (15, 55, 0), (15, 56, 0))
    assert start == "2025-12-15T20:55:00Z"
    assert end == "2025-12-15T20:56:00Z"


# ── aggregate ────────────────────────────────────────────────────────────────

def _make_event(direction, open_hs, close_hs):
    return {
        "ticker": "AAPL",
        "date": "2025-06-01",
        "direction": direction,
        "open_hs": open_hs,
        "close_hs": close_hs,
        "incr": open_hs - close_hs,
    }


def test_aggregate_known_values():
    events = [
        _make_event("long", 10.0, 5.0),    # incr=5
        _make_event("long", 20.0, 10.0),   # incr=10
        _make_event("long", 30.0, 15.0),   # incr=15
        _make_event("short", 8.0, 4.0),    # incr=4
        _make_event("short", 12.0, 6.0),   # incr=6
        _make_event("short", 16.0, 8.0),   # incr=8
    ]
    agg = m.aggregate(events)

    assert agg["long"]["n"] == 3
    assert abs(agg["long"]["incr_mean"] - 10.0) < 1e-9
    assert abs(agg["long"]["incr_median"] - 10.0) < 1e-9
    # p25 of [5,10,15] (linear) = 7.5
    assert abs(agg["long"]["incr_p25"] - 7.5) < 1e-6
    assert abs(agg["long"]["incr_p75"] - 12.5) < 1e-6

    assert agg["short"]["n"] == 3
    assert abs(agg["short"]["incr_mean"] - 6.0) < 1e-9
    assert abs(agg["short"]["open_hs_mean"] - 12.0) < 1e-9


def test_aggregate_empty_direction_returns_zero_n():
    # Only long events, no short events
    events = [_make_event("long", 10.0, 5.0) for _ in range(5)]
    agg = m.aggregate(events)
    assert agg["short"]["n"] == 0
    assert agg["short"]["incr_mean"] is None


def test_aggregate_empty_events_both_zero():
    agg = m.aggregate([])
    assert agg["long"]["n"] == 0
    assert agg["short"]["n"] == 0


# ── verdict ───────────────────────────────────────────────────────────────────

def _agg_with_incr(long_incr, short_incr, long_n=400, short_n=400):
    """Build a minimal aggregate dict with given mean/median incr for verdict."""
    def _dir(incr, n):
        return {
            "n": n,
            "incr_mean": incr,
            "incr_median": incr,
        }
    return {"long": _dir(long_incr, long_n), "short": _dir(short_incr, short_n)}


def test_verdict_invalid_data_long_under_300():
    agg = _agg_with_incr(1.0, 1.0, long_n=299, short_n=400)
    v = m.verdict(agg)
    assert v["label"] == "INVALID-DATA"
    assert v["long_net_mean"] is None


def test_verdict_invalid_data_short_under_300():
    agg = _agg_with_incr(1.0, 1.0, long_n=400, short_n=100)
    v = m.verdict(agg)
    assert v["label"] == "INVALID-DATA"


def test_verdict_park_when_incr_large():
    # short_gross=1.940; if incr>1.940 then short_net<0 -> PARK
    agg = _agg_with_incr(2.0, 2.5)   # short_net_mean=1.940-2.5=-0.56
    v = m.verdict(agg)
    assert v["label"] == "PARK"
    assert v["short_net_mean"] < 0
    assert v["short_net_median"] < 0


def test_verdict_ship_shorts_when_incr_tiny():
    # short_gross=1.940; if incr=0.1, short_net=1.84 > 0.5 -> SHIP-SHORTS-CANDIDATE
    agg = _agg_with_incr(0.5, 0.1)
    v = m.verdict(agg)
    assert v["label"] == "SHIP-SHORTS-CANDIDATE"
    assert v["short_net_median"] > m.SHIP_SHORT_BPS


def test_verdict_marginal_boundary():
    # short_net_median exactly 0.4 (between 0 and 0.5) -> MARGINAL
    # short_net_median = 1.940 - incr_median; incr=1.540 => net=0.40
    agg = _agg_with_incr(1.0, 1.540)
    v = m.verdict(agg)
    assert v["label"] == "MARGINAL"
    assert 0 < v["short_net_median"] <= m.SHIP_SHORT_BPS


def test_verdict_net_values_correct():
    # long_incr=1.0 -> long_net=0.098-1.0=-0.902; short_incr=0.5 -> short_net=1.940-0.5=1.44
    agg = _agg_with_incr(1.0, 0.5)
    v = m.verdict(agg)
    assert abs(v["long_net_mean"] - (m.LONG_GROSS_EDGE_BPS - 1.0)) < 1e-9
    assert abs(v["short_net_mean"] - (m.SHORT_GROSS_EDGE_BPS - 0.5)) < 1e-9
    assert v["label"] == "SHIP-SHORTS-CANDIDATE"


# ── runner import smoke ───────────────────────────────────────────────────────

def _load_runner():
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "run_open_spread_study",
        root / "scripts" / "run_open_spread_study.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_render_report_contains_verdict_and_tables():
    runner = _load_runner()
    events = []
    for i in range(350):
        events.append({
            "ticker": f"T{i}", "date": "2025-06-01", "direction": "long",
            "open_hs": 15.0, "close_hs": 10.0, "incr": 5.0,
        })
    for i in range(350):
        events.append({
            "ticker": f"T{i}", "date": "2025-06-01", "direction": "short",
            "open_hs": 5.0, "close_hs": 4.0, "incr": 1.0,
        })
    agg = m.aggregate(events)
    v = m.verdict(agg)
    md = runner.render_report(agg, v)
    assert "VERDICT:" in md
    assert v["label"] in md
    assert "open_hs" in md or "open" in md.lower()
    assert "incr" in md
    assert "long_net" in md or "long" in md


# ── mocked subprocess tests (pin _pull_quotes contract) ──────────────────────

import json as _json
import unittest.mock as _mock


def test_pull_quotes_success_parses_list():
    """Mock a successful CLI response: stdout is a JSON array of quote dicts."""
    runner = _load_runner()
    # Realistic .quotes output: a JSON list of quote objects with ap/bp
    quotes_payload = [
        {"ap": 150.02, "bp": 149.98, "t": "2025-07-01T13:31:05Z"},
        {"ap": 150.04, "bp": 149.96, "t": "2025-07-01T13:31:15Z"},
        {"ap": 150.03, "bp": 149.97, "t": "2025-07-01T13:31:30Z"},
    ]
    mock_result = _mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _json.dumps(quotes_payload)

    with _mock.patch("subprocess.run", return_value=mock_result) as mock_run:
        result = runner._pull_quotes("AAPL", "2025-07-01T13:31:00Z", "2025-07-01T13:32:00Z")

    assert result is not None
    assert len(result) == 3
    assert result[0]["ap"] == 150.02
    # Verify the CLI command shape
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == runner._ALPACA_BIN
    assert "--symbol" in cmd
    assert "AAPL" in cmd
    assert "--jq" in cmd
    assert ".quotes" in cmd
    assert "--quiet" in cmd
    assert "--limit" in cmd


def test_pull_quotes_nonzero_exit_returns_none():
    """Non-zero exit code triggers retries then returns None."""
    runner = _load_runner()
    mock_result = _mock.MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""

    with _mock.patch("subprocess.run", return_value=mock_result):
        with _mock.patch("time.sleep"):  # skip actual sleeps in test
            result = runner._pull_quotes("AAPL", "2025-07-01T13:31:00Z", "2025-07-01T13:32:00Z")

    assert result is None


def test_compute_event_valid_event_from_mocked_quotes():
    """_compute_event with mocked subprocess produces a valid event with correct incr.

    Quote math check:
      ap=10.02, bp=9.98 -> half=(0.04/2)/10.00 * 1e4 = 20 bps
      ap=10.01, bp=9.99 -> half=(0.02/2)/10.00 * 1e4 = 10 bps
      incr = 20 - 10 = 10 bps
    """
    runner = _load_runner()
    # Open quotes: ap/bp -> 20 bps half-spread (3 quotes)
    open_quotes = [
        {"ap": 10.02, "bp": 9.98},  # half=(0.02/10)*1e4 = 20 bps
        {"ap": 10.02, "bp": 9.98},
        {"ap": 10.02, "bp": 9.98},
    ]
    # Close quotes: 10 bps half-spread (3 quotes)
    close_quotes = [
        {"ap": 10.01, "bp": 9.99},  # half=(0.01/10)*1e4 = 10 bps
        {"ap": 10.01, "bp": 9.99},
        {"ap": 10.01, "bp": 9.99},
    ]
    # alternating responses: first call = open, second call = close
    call_count = {"n": 0}

    def _fake_run(*args, **kwargs):
        r = _mock.MagicMock()
        r.returncode = 0
        if call_count["n"] == 0:
            r.stdout = _json.dumps(open_quotes)
        else:
            r.stdout = _json.dumps(close_quotes)
        call_count["n"] += 1
        return r

    with _mock.patch("subprocess.run", side_effect=_fake_run):
        with _mock.patch("time.sleep"):
            event = runner._compute_event("AAPL", "2025-07-01", "long")

    assert event["valid"] is True
    assert event["ticker"] == "AAPL"
    assert event["direction"] == "long"
    assert event["open_hs"] is not None
    assert event["close_hs"] is not None
    assert abs(event["incr"] - (event["open_hs"] - event["close_hs"])) < 1e-9
    assert abs(event["open_hs"] - 20.0) < 1e-6
    assert abs(event["close_hs"] - 10.0) < 1e-6
    assert abs(event["incr"] - 10.0) < 1e-6
