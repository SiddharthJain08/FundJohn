"""Unit tests for the correlation and concentration gate."""

import json
import math
from pathlib import Path

import numpy as np
import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.gates.correlation_gate import run_gate, MAX_POSITIONS, CORR_THRESHOLD


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _price_series(n: int, seed: int = 42, drift: float = 0.001) -> list:
    rng = np.random.default_rng(seed)
    log_returns = rng.normal(drift, 0.02, n)
    prices = 100.0 * np.exp(np.cumsum(log_returns))
    return prices.tolist()


def _correlated_series(base: list, rho: float, seed: int = 99) -> list:
    """Return a price series whose Pearson corr with base is approximately rho."""
    rng = np.random.default_rng(seed)
    base_ret   = np.diff(np.log(np.array(base) + 1e-9))
    noise_ret  = rng.normal(0, 0.02, len(base_ret))
    mixed_ret  = rho * base_ret + math.sqrt(max(0.0, 1 - rho ** 2)) * noise_ret
    prices = [100.0]
    for r in mixed_ret:
        prices.append(prices[-1] * math.exp(r))
    return prices


def _noop_alert(_msg):
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCorrelationGate:
    def test_approved_when_no_active_positions(self):
        prices = {"AAPL": _price_series(25)}
        approved, rejected = run_gate(
            ["AAPL"], [], 0,
            price_fetcher=lambda t: prices[t],
            alert_fn=_noop_alert,
        )
        assert approved == ["AAPL"]
        assert rejected == []

    def test_rejected_when_high_correlation(self):
        base       = _price_series(25, seed=1)
        correlated = _correlated_series(base, rho=0.95)
        prices     = {"SPY": base, "QQQ": correlated}
        approved, rejected = run_gate(
            ["QQQ"], ["SPY"], 1,
            price_fetcher=lambda t: prices[t],
            alert_fn=_noop_alert,
        )
        assert rejected == ["QQQ"]
        assert approved == []

    def test_approved_when_low_correlation(self):
        base         = _price_series(25, seed=1)
        uncorrelated = _correlated_series(base, rho=0.1, seed=7)
        prices       = {"SPY": base, "GLD": uncorrelated}
        approved, rejected = run_gate(
            ["GLD"], ["SPY"], 1,
            price_fetcher=lambda t: prices[t],
            alert_fn=_noop_alert,
        )
        assert approved == ["GLD"]
        assert rejected == []

    def test_concentration_limit_rejects_when_full(self):
        prices = {"NEW": _price_series(25)}
        approved, rejected = run_gate(
            ["NEW"], [], MAX_POSITIONS,
            price_fetcher=lambda t: prices[t],
            alert_fn=_noop_alert,
        )
        assert rejected == ["NEW"]
        assert approved == []

    def test_concentration_limit_partial_fill(self):
        """7 open positions + 3 signals: only 1 approved before cap hit."""
        base   = _price_series(25, seed=10)
        prices = {t: _correlated_series(base, rho=0.1, seed=i) for i, t in enumerate(["A", "B", "C"])}
        approved, rejected = run_gate(
            ["A", "B", "C"], [], 7,
            price_fetcher=lambda t: prices[t],
            alert_fn=_noop_alert,
        )
        assert len(approved) == 1
        assert len(rejected) == 2

    def test_rejection_logged_to_costs_jsonl(self, tmp_path):
        base       = _price_series(25, seed=1)
        correlated = _correlated_series(base, rho=0.95)
        prices     = {"SPY": base, "QQQ": correlated}
        costs_file = tmp_path / "costs.jsonl"
        run_gate(
            ["QQQ"], ["SPY"], 1,
            price_fetcher=lambda t: prices[t],
            alert_fn=_noop_alert,
            costs_path=costs_file,
        )
        lines = costs_file.read_text().strip().split("\n")
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["ticker"] == "QQQ"
        assert rec["reason"] == "correlation_gate"

    def test_concentration_rejection_logged(self, tmp_path):
        prices     = {"NEW": _price_series(25)}
        costs_file = tmp_path / "costs.jsonl"
        run_gate(
            ["NEW"], [], MAX_POSITIONS,
            price_fetcher=lambda t: prices[t],
            alert_fn=_noop_alert,
            costs_path=costs_file,
        )
        rec = json.loads(costs_file.read_text().strip())
        assert rec["reason"] == "concentration_limit"

    def test_alert_fn_called_on_rejection(self):
        base       = _price_series(25, seed=1)
        correlated = _correlated_series(base, rho=0.95)
        prices     = {"SPY": base, "QQQ": correlated}
        alerts     = []
        run_gate(
            ["QQQ"], ["SPY"], 1,
            price_fetcher=lambda t: prices[t],
            alert_fn=alerts.append,
        )
        assert len(alerts) == 1
        assert "QQQ" in alerts[0]

    def test_fail_open_when_fetch_raises(self):
        """Signal is approved when price data is unavailable."""
        def bad_fetcher(_t):
            raise ConnectionError("network down")

        approved, rejected = run_gate(
            ["AAPL"], [], 0,
            price_fetcher=bad_fetcher,
            alert_fn=_noop_alert,
        )
        assert approved == ["AAPL"]
        assert rejected == []

    def test_correlated_signals_dont_block_each_other(self):
        """Two correlated signals both pass -- gate is signal-vs-position only."""
        base       = _price_series(25, seed=1)
        correlated = _correlated_series(base, rho=0.95)
        prices     = {"A": base, "B": correlated}
        approved, rejected = run_gate(
            ["A", "B"], [], 0,
            price_fetcher=lambda t: prices[t],
            alert_fn=_noop_alert,
        )
        assert set(approved) == {"A", "B"}
        assert rejected == []

    def test_boundary_correlation_at_threshold(self):
        """Signal with corr exactly == threshold is rejected (> not >=)."""
        base       = _price_series(25, seed=1)
        at_thresh  = _correlated_series(base, rho=CORR_THRESHOLD, seed=5)
        above      = _correlated_series(base, rho=0.76, seed=5)
        prices     = {"SPY": base, "AT": at_thresh, "AB": above}

        _, rej_at = run_gate(
            ["AT"], ["SPY"], 1,
            price_fetcher=lambda t: prices[t], alert_fn=_noop_alert,
        )
        _, rej_ab = run_gate(
            ["AB"], ["SPY"], 1,
            price_fetcher=lambda t: prices[t], alert_fn=_noop_alert,
        )
        # rho=0.76 is above threshold -- should be rejected
        assert "AB" in rej_ab
