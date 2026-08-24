"""Task P2 — strategy-level HRP shadow + ticker-level gross-scaling fix.

Pure-function / synthetic-panel tests only. All DB-backed loaders
(strategy_similarity._current_weight_rows / _returns_by_regime /
_returns_by_regime_backtest) are monkeypatched; no real DB is touched.

Covers:
  1. Ticker-level gross-scaling fix (shadow_run): a fully-invested HRP
     allocation scaled down to the live book's realized gross before the
     diff is computed (exact-value, hand-built 4-ticker case).
  2. Strategy-level HRP (shadow_run_strategy): weights sum to 1, the
     highest-vol strategy gets the lowest weight on a near-diagonal
     covariance case; <2 strategies -> skip (returns None).
  3. The runner's panel builder (_build_strategy_return_panel): current-
     weight-set restriction, the >=60-obs floor with live-preferred /
     backtest-fallback per strategy, and the flat-day (missing -> 0.0) fill.
  4. Review finding 1 (in-window observation floor): a strategy whose
     observations all predate the trimmed window is excluded even though it
     clears the >=60 LIFETIME floor, and obs_in_window is persisted into the
     row's JSON.
  5. Review finding 2 (ordering guarantee): the ticker-level ('hrp') row
     persists strictly before the strategy-level attempt, and a total
     failure of the strategy-level step never propagates out.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.execution.pyportfolioopt_shadow_sizer import (  # noqa: E402
    scale_weights_to_live_gross,
    shadow_run,
    shadow_run_strategy,
)

# Load scripts/run_pyportfolioopt_shadow.py by path (it isn't an importable
# package) — mirrors tests/scripts/test_redeploy_pipeline.py's convention.
_spec = importlib.util.spec_from_file_location(
    "run_pyportfolioopt_shadow", ROOT / "scripts" / "run_pyportfolioopt_shadow.py"
)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

from execution import strategy_similarity as ssim  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────
# 1. Ticker-level gross-scaling fix
# ─────────────────────────────────────────────────────────────────────────

def _dummy_ticker_returns(tickers, n_days=30, seed=1):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(0.0, 0.01, size=(n_days, len(tickers))), columns=tickers)


def test_scale_weights_to_live_gross_exact():
    """Pure function: unit weights scaled to the live book's realized gross,
    expressed as a fraction of equity."""
    weights = {"A": 0.1, "B": 0.2, "C": 0.3, "D": 0.4}
    live_dollars = {"A": 4000.0, "B": 3000.0, "C": 2000.0, "D": 1000.0}  # gross = $10k
    equity = 100_000.0

    scaled, live_gross = scale_weights_to_live_gross(weights, live_dollars, equity)

    assert live_gross == pytest.approx(10_000.0)
    # scaled weights, expressed in dollars (equity * scaled), sum to live_gross
    scaled_dollars = {k: equity * w for k, w in scaled.items()}
    assert sum(scaled_dollars.values()) == pytest.approx(10_000.0)
    assert scaled_dollars == pytest.approx({"A": 1000.0, "B": 2000.0, "C": 3000.0, "D": 4000.0})
    assert scaled == pytest.approx({"A": 0.01, "B": 0.02, "C": 0.03, "D": 0.04})


def test_scale_weights_to_live_gross_zero_equity_is_safe():
    weights = {"A": 0.5, "B": 0.5}
    scaled, live_gross = scale_weights_to_live_gross(weights, {"A": 100.0}, equity=0.0)
    assert scaled == {"A": 0.0, "B": 0.0}
    assert live_gross == pytest.approx(100.0)


def test_shadow_run_gross_scaling_exact_values(monkeypatch):
    """End-to-end shadow_run(): fixed HRP weights + a hand-built 4-ticker live
    book -> exact target_dollars/diff_dollars/diff_weights, and the raw
    unit-sum weights are preserved separately under hrp_weights_unit."""
    import src.execution.pyportfolioopt_shadow_sizer as sizer

    fixed_weights = {"A": 0.1, "B": 0.2, "C": 0.3, "D": 0.4}
    monkeypatch.setattr(sizer, "allocate_hrp", lambda returns: dict(fixed_weights))

    live_dollars = {"A": 4000.0, "B": 3000.0, "C": 2000.0, "D": 1000.0}  # gross = $10k
    equity = 100_000.0
    handoff = {"signals": [], "portfolio": {"portfolio_value": equity}}
    returns = _dummy_ticker_returns(list(fixed_weights))

    result = sizer.shadow_run(handoff, returns, live_dollars)

    assert result["method"] == "hrp"
    assert result["hrp_weights_unit"] == pytest.approx(fixed_weights)
    # target_dollars scaled to live gross ($10k), NOT to equity ($100k, which
    # would be the pre-fix, fully-invested behaviour).
    assert result["target_dollars"] == pytest.approx(
        {"A": 1000.0, "B": 2000.0, "C": 3000.0, "D": 4000.0})
    assert sum(result["target_dollars"].values()) == pytest.approx(10_000.0)
    assert result["diff_dollars"] == pytest.approx(
        {"A": -3000.0, "B": -1000.0, "C": 1000.0, "D": 3000.0})
    # diff_weights computed on the SAME (equity) footing on both sides.
    assert result["weights"] == pytest.approx({"A": 0.01, "B": 0.02, "C": 0.03, "D": 0.04})
    assert result["diff_weights"] == pytest.approx(
        {"A": -0.03, "B": -0.01, "C": 0.01, "D": 0.03})
    assert result["live_gross_usd"] == pytest.approx(10_000.0)


# ─────────────────────────────────────────────────────────────────────────
# 2. Strategy-level HRP
# ─────────────────────────────────────────────────────────────────────────

def _synthetic_strategy_panel(n_days=300, n_strats=5, dominant_idx=4, seed=11):
    """Near-diagonal (independent) daily-return panel: one strategy carries
    much higher volatility than the rest, so HRP's recursive bisection
    should behave like inverse-variance allocation and give it the smallest
    weight."""
    rng = np.random.default_rng(seed)
    cols = [f"S{i}" for i in range(n_strats)]
    data = {}
    for i, c in enumerate(cols):
        scale = 0.05 if i == dominant_idx else 0.004
        data[c] = rng.normal(0.0, scale, size=n_days)
    return pd.DataFrame(data, index=pd.date_range("2025-01-01", periods=n_days, freq="B"))


def test_shadow_run_strategy_weights_sum_to_one_and_inverse_variance():
    panel = _synthetic_strategy_panel()
    live_weights = {f"S{i}": 1.0 for i in range(5)}  # uniform live-implied weight
    handoff = {"signals": []}

    result = shadow_run_strategy(handoff, panel, live_weights)

    assert result is not None
    assert result["method"] == "hrp_strategy"
    weights = result["weights"]
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
    dominant = "S4"
    assert weights[dominant] == min(weights.values()), weights
    # live_dollars holds the normalized |daily_weight| distribution (uniform
    # input here -> uniform output) and diff_weights = weights - live_dollars.
    assert result["live_dollars"] == pytest.approx({f"S{i}": 0.2 for i in range(5)})
    for s in weights:
        assert result["diff_weights"][s] == pytest.approx(weights[s] - 0.2)


def test_shadow_run_strategy_uses_abs_of_signed_daily_weight():
    panel = _synthetic_strategy_panel(n_strats=3, dominant_idx=0, seed=3)
    # signed daily_weight (a short-leaning strategy can carry a negative
    # sizer weight) — comparison target is normalized |daily_weight|.
    live_weights = {"S0": -2.0, "S1": 1.0, "S2": 1.0}
    handoff = {"signals": []}
    result = shadow_run_strategy(handoff, panel, live_weights)
    assert result is not None
    assert result["live_dollars"] == pytest.approx({"S0": 0.5, "S1": 0.25, "S2": 0.25})


def test_shadow_run_strategy_skips_below_two_strategies():
    single_col_panel = pd.DataFrame({"S0": np.random.default_rng(0).normal(0, 0.01, 100)})
    assert shadow_run_strategy({"signals": []}, single_col_panel, {"S0": 1.0}) is None

    empty_panel = pd.DataFrame()
    assert shadow_run_strategy({"signals": []}, empty_panel, {}) is None


# ─────────────────────────────────────────────────────────────────────────
# 3. Runner: strategy-return panel builder (loaders monkeypatched)
# ─────────────────────────────────────────────────────────────────────────

def _dates(n, start="2026-01-01"):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


def test_build_strategy_return_panel_filters_and_fills_flat_days(monkeypatch):
    regime = "LOW_VOL"

    # Current-weight set for LOW_VOL: 3 strategies carry a weight row.
    weight_rows = {
        "alpha": {"daily_weight": 1.5, "bt_n": 100, "bt_sharpe": 1.2},
        "beta":  {"daily_weight": 0.8, "bt_n": 50,  "bt_sharpe": 0.9},
        "gamma": {"daily_weight": 0.3, "bt_n": 10,  "bt_sharpe": 0.2},
    }
    monkeypatch.setattr(ssim, "_current_weight_rows", lambda rs: weight_rows if rs == regime else {})

    # alpha: 70 live observations (clears the 60-obs floor).
    alpha_dates = _dates(70)
    live_returns_by_regime = {
        regime: {
            "alpha": {d: 0.001 * i for i, d in enumerate(alpha_dates)},
            # beta: only 10 live observations -> must fall back to backtest.
            "beta": {d: 0.002 for d in _dates(10)},
            # gamma: no live observations at all.
        }
    }
    monkeypatch.setattr(ssim, "_returns_by_regime", lambda window_days: live_returns_by_regime)

    bt_dates = _dates(65, start="2025-01-01")
    bt_returns_by_regime = {
        regime: {
            "beta": {d: 0.003 for d in bt_dates},   # 65 >= 60 -> qualifies via fallback
            "gamma": {d: 0.001 for d in bt_dates[:30]},  # only 30 -> still short, excluded
        }
    }
    monkeypatch.setattr(ssim, "_returns_by_regime_backtest", lambda: bt_returns_by_regime)

    panel, live_weights, obs_in_window = runner._build_strategy_return_panel(regime)

    # gamma excluded (no source clears the 60-obs floor); alpha+beta remain.
    assert set(panel.columns) == {"alpha", "beta"}
    assert set(live_weights) == {"alpha", "beta"}
    assert live_weights["alpha"] == pytest.approx(1.5)
    assert live_weights["beta"] == pytest.approx(0.8)
    # in-window obs counts: alpha's 70 live obs and beta's 65 backtest obs
    # both fall entirely within the (small, <252-date) union window here.
    assert obs_in_window == {"alpha": 70, "beta": 65}

    # flat-day convention: dates present for alpha but not beta (or vice
    # versa) are filled 0.0, not dropped — the panel's date axis is the
    # union of both strategies' contributing dates.
    assert not panel.isna().any().any()
    missing_for_beta = set(alpha_dates) - set(bt_dates)
    present_missing = [d for d in missing_for_beta if d in panel.index]
    assert present_missing, "expected at least one alpha-only date in the panel"
    for d in present_missing:
        assert panel.loc[d, "beta"] == 0.0


def test_build_strategy_return_panel_skips_below_two_strategies(monkeypatch):
    regime = "CRISIS"
    monkeypatch.setattr(ssim, "_current_weight_rows",
                        lambda rs: {"solo": {"daily_weight": 1.0}} if rs == regime else {})
    monkeypatch.setattr(ssim, "_returns_by_regime",
                        lambda window_days: {regime: {"solo": {d: 0.001 for d in _dates(100)}}})
    monkeypatch.setattr(ssim, "_returns_by_regime_backtest", lambda: {regime: {}})

    panel, live_weights, obs_in_window = runner._build_strategy_return_panel(regime)
    assert panel.empty
    assert live_weights == {}
    assert obs_in_window == {}


def test_build_strategy_return_panel_no_current_weights(monkeypatch):
    monkeypatch.setattr(ssim, "_current_weight_rows", lambda rs: {})
    panel, live_weights, obs_in_window = runner._build_strategy_return_panel("HIGH_VOL")
    assert panel.empty
    assert live_weights == {}
    assert obs_in_window == {}


# ─────────────────────────────────────────────────────────────────────────
# 4. Review finding 1 — in-window observation floor
# ─────────────────────────────────────────────────────────────────────────

def test_build_strategy_return_panel_drops_strategy_whose_obs_predate_window(monkeypatch, capsys):
    """A strategy can clear the >=60 LIFETIME observation floor (the check
    that picks live vs. backtest as its source) while having ALL of those
    observations fall before the final, most-recent 252-day trading window
    once the panel is trimmed -- e.g. a backtest-fallback strategy whose
    trade history is a decade old. Pre-fix, that strategy would enter the
    panel as an all-zero (post-fillna) column and only get dropped by the
    old "any nonzero" guard; this test builds that case explicitly and
    checks the NEW in-window recount catches it (and logs it), while a
    strategy with a full 100 observations actually inside the window stays.
    """
    regime = "LOW_VOL"
    weight_rows = {
        "recent":  {"daily_weight": 1.0},
        "partial": {"daily_weight": 0.5},
        "old":     {"daily_weight": 2.0},
    }
    monkeypatch.setattr(ssim, "_current_weight_rows", lambda rs: weight_rows if rs == regime else {})

    # "recent": dense coverage across a wide, recent business-day range —
    # 300 trading days starting 2025-01-01, well over STRATEGY_MIN_OBS and
    # spanning the whole eventual trimmed (last-252) window.
    recent_dates = _dates(300, start="2025-01-01")
    # "partial": exactly 100 observations, all a SUBSET of recent_dates'
    # most recent tail -> guaranteed to land entirely inside the trimmed
    # window -> clears both the lifetime AND in-window floors -> must stay.
    partial_dates = recent_dates[-100:]
    # "old": 90 LIFETIME observations (clears the >=60 lifetime floor, so it
    # is NOT excluded by the earlier per-source selection step) but every
    # one of them is a decade before "recent"/"partial" -> once the panel is
    # trimmed to the most-recent 252 dates, none of "old"'s dates survive
    # -> 0 obs in window -> must be excluded by the NEW floor.
    old_dates = _dates(90, start="2015-01-01")

    live_returns_by_regime = {
        regime: {
            "recent":  {d: 0.001 for d in recent_dates},
            "partial": {d: 0.002 for d in partial_dates},
            "old":     {d: 0.003 for d in old_dates},
        }
    }
    monkeypatch.setattr(ssim, "_returns_by_regime", lambda window_days: live_returns_by_regime)
    monkeypatch.setattr(ssim, "_returns_by_regime_backtest", lambda: {regime: {}})

    panel, live_weights, obs_in_window = runner._build_strategy_return_panel(regime)

    # "old" cleared the lifetime floor (90 >= 60) but is excluded here because
    # none of its observations fall inside the trimmed window.
    assert set(panel.columns) == {"recent", "partial"}
    assert "old" not in panel.columns
    assert set(live_weights) == {"recent", "partial"}
    assert "old" not in live_weights

    # "partial" has exactly 100 in-window observations and stays.
    assert obs_in_window["partial"] == 100
    assert obs_in_window["recent"] == runner.STRATEGY_RETURNS_LOOKBACK_DAYS
    assert "old" not in obs_in_window

    # Dropped strategy is logged with its in-window count.
    err = capsys.readouterr().err
    assert "[hrp_strategy] dropped old: 0/" in err


def test_persist_includes_obs_in_window_in_weights_payload():
    """_persist writes obs_in_window (when present on the result dict) into
    the same 'weights' JSONB column payload as 'weights' -- so an operator
    reading pyportfolioopt_shadow_runs can judge per-strategy data density
    without a schema change. Uses a fake conn/cursor -- no real DB."""

    class _FakeCursor:
        def __init__(self):
            self.executed = None

        def execute(self, sql, params):
            self.executed = (sql, params)

    class _FakeConn:
        def __init__(self):
            self.cur = _FakeCursor()
            self.committed = False

        def cursor(self):
            return self.cur

        def commit(self):
            self.committed = True

    result = {
        "method": "hrp_strategy",
        "handoff_signals_n": 0,
        "equity_usd": 100_000.0,
        "weights": {"alpha": 0.6, "beta": 0.4},
        "target_dollars": {"alpha": 0.6, "beta": 0.4},
        "live_dollars": {"alpha": 0.5, "beta": 0.5},
        "diff_dollars": {"alpha": 0.1, "beta": -0.1},
        "diff_weights": {"alpha": 0.1, "beta": -0.1},
        "diversification_ratio": 1.1,
        "expected_vol_pct": 5.0,
        "obs_in_window": {"alpha": 252, "beta": 100},
    }

    fake_conn = _FakeConn()
    runner._persist(fake_conn, "2026-08-24", result, "notes")

    assert fake_conn.committed
    _, params = fake_conn.cur.executed
    weights_payload = json.loads(params[4])
    assert weights_payload["weights"] == {"alpha": 0.6, "beta": 0.4}
    assert weights_payload["obs_in_window"] == {"alpha": 252, "beta": 100}
    assert "hrp_weights_unit" not in weights_payload

    # method='hrp' rows without obs_in_window omit the key entirely (no
    # spurious {} noise on every ticker-level row).
    ticker_result = dict(result, method="hrp", obs_in_window={})
    fake_conn2 = _FakeConn()
    runner._persist(fake_conn2, "2026-08-24", ticker_result, "notes")
    _, params2 = fake_conn2.cur.executed
    assert "obs_in_window" not in json.loads(params2[4])


# ─────────────────────────────────────────────────────────────────────────
# 5. Review finding 2 — ordering guarantee (ticker persist before, and
#    surviving, strategy-level)
# ─────────────────────────────────────────────────────────────────────────

class _NoopConn:
    def close(self):
        pass


def test_run_ticker_then_strategy_persists_ticker_row_before_strategy_level(monkeypatch):
    """The ticker-level ('hrp') _persist call must happen, and must happen
    BEFORE _run_strategy_level is invoked -- exercised directly (task-P2
    review finding 2), not just relied on by code order. Also asserts a
    total failure of _run_strategy_level (here a fully-replaced, raising
    stub -- bypassing its own internal try/except entirely) does not
    propagate out of _run_ticker_then_strategy and does not affect the
    ticker persist that already happened.
    """
    calls: list[str] = []

    def fake_persist(conn, run_date, result, notes):
        calls.append(f"persist:{result['method']}")

    def fake_run_strategy_level(conn, run_date, handoff, pg_uri):
        calls.append("run_strategy_level")
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_persist", fake_persist)
    monkeypatch.setattr(runner, "_run_strategy_level", fake_run_strategy_level)
    monkeypatch.setattr(psycopg2, "connect", lambda uri: _NoopConn())

    handoff = {"signals": []}
    result = {"method": "hrp", "diff_dollars": {}, "diff_weights": {},
              "diversification_ratio": None, "expected_vol_pct": 0.0}

    suffix = runner._run_ticker_then_strategy(
        "postgresql://fake", "2026-08-24", handoff, result, "notes")

    # Ticker persist ran, and ran before the (raising) strategy-level call.
    assert calls == ["persist:hrp", "run_strategy_level"]
    # No exception propagated; a skip suffix came back instead.
    assert suffix.startswith(" | strat_hrp n=0 (skipped: unexpected error")
    assert "boom" in suffix


def test_run_ticker_then_strategy_propagates_ticker_persist_failure(monkeypatch):
    """By contrast, a failure in the TICKER persist itself is not swallowed
    -- only the strategy-level half is best-effort. _run_strategy_level must
    never even be attempted in that case."""
    calls: list[str] = []

    def failing_persist(conn, run_date, result, notes):
        raise RuntimeError("ticker persist boom")

    def fake_run_strategy_level(conn, run_date, handoff, pg_uri):
        calls.append("run_strategy_level")
        return " | strat_hrp n=0 (skipped: unreached)"

    monkeypatch.setattr(runner, "_persist", failing_persist)
    monkeypatch.setattr(runner, "_run_strategy_level", fake_run_strategy_level)
    monkeypatch.setattr(psycopg2, "connect", lambda uri: _NoopConn())

    with pytest.raises(RuntimeError, match="ticker persist boom"):
        runner._run_ticker_then_strategy(
            "postgresql://fake", "2026-08-24", {"signals": []},
            {"method": "hrp"}, "notes")

    assert calls == []
