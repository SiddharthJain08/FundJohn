"""Tests for src/research/bflow/xsec_discriminator.py — TDD (write first).

Helpers mirror tests/test_bflow_mr_policy_pair.py conventions.
"""
from __future__ import annotations

import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _bar(m, p, v=1000.0):
    return {"minute": m, "o": p, "h": p + 0.2, "l": p - 0.2,
            "c": p, "v": v, "vw": p}


def _make_frame(prices):
    """390-bar DataFrame from a list of 390 prices; vw=o=h±0.2=l=c=p."""
    rows = [_bar(m, p) for m, p in enumerate(prices)]
    return pd.DataFrame(rows)


def _flat_prices():
    return [100.0] * 390


def _dip_prices(dip_start=60, dip_end=120, dip_val=95.0):
    return [dip_val if dip_start <= m <= dip_end else 100.0
            for m in range(390)]


# ---------------------------------------------------------------------------
# 1. common-drift world — gross == 0, net != 0 (cost drag)
#
# NOTE (BLOCKED finding): The spec asserts spread_net == 0 in this world.
# Numerically it is -2·C_mean ≈ -0.36 bps because the differential cost
# C = spread(fill_bar) - dump_spread is ~1.05bps inside the dip window;
# both legs pay it, so net = -2·C per minute, while gross = 0.
# This test asserts the spec expectation for gross (correct) and net (incorrect
# per the frozen cost model). The net assertion WILL FAIL on a correct
# implementation. Diagnosis: spec error — net ≈ -0.36bps, not 0. Do not
# tune the module; report BLOCKED.
# ---------------------------------------------------------------------------
def test_common_drift_world_spread_zero():
    from research.bflow.xsec_discriminator import session_spreads

    prices = _dip_prices(60, 120)
    frames = {f"T{i:02d}": _make_frame(prices) for i in range(60)}

    row = session_spreads(frames, "2024-01-02")
    assert row is not None, "expected eligible session"
    assert row["n_minutes"] >= 100

    # gross == 0: all tickers identical → G[lo] == G[hi] → difference cancels
    assert np.isclose(row["spread_gross"], 0.0, atol=1e-9), (
        f"spread_gross should be 0 for identical tickers, got {row['spread_gross']}")

    # net == 0: SPEC ASSERTION — will fail on a correct implementation because
    # net = -2·C_mean ≈ -0.36 bps (cost drag; both legs pay differential spread).
    # BLOCKED: this assertion is incorrect per the frozen cost model.
    assert np.isclose(row["spread_net"], 0.0, atol=1e-9), (
        f"BLOCKED: spread_net expected 0 per spec but got {row['spread_net']:.6f}; "
        "for identical tickers net = -2·C ≈ -0.36bps (both legs pay diff spread); "
        "spec assertion appears to be an error in the prereg")


# ---------------------------------------------------------------------------
# 2. cross-sectional world — spread_gross > 0
# ---------------------------------------------------------------------------
def test_cross_sectional_world_positive_spread():
    from research.bflow.xsec_discriminator import session_spreads

    frames = {}
    # 30 dippers: close/dump=100, dip to 95-i*0.01 at minutes 60..200 then recover
    for i in range(30):
        dip_val = 95.0 - i * 0.01
        prices = [dip_val if 60 <= m <= 200 else 100.0 for m in range(390)]
        frames[f"D{i:02d}"] = _make_frame(prices)
    # 30 rippers: rise to 105+i*0.01 at minutes 60..200 then fall back
    for i in range(30):
        rip_val = 105.0 + i * 0.01
        prices = [rip_val if 60 <= m <= 200 else 100.0 for m in range(390)]
        frames[f"R{i:02d}"] = _make_frame(prices)

    row = session_spreads(frames, "2024-01-02")
    assert row is not None, "expected eligible session"
    assert row["spread_gross"] > 0, (
        f"expected spread_gross > 0 in cross-sectional world, got {row['spread_gross']:.4f}")


# ---------------------------------------------------------------------------
# 3. minute gate — n_xs < 50 → None
# ---------------------------------------------------------------------------
def test_minute_gate_n_xs():
    from research.bflow.xsec_discriminator import session_spreads

    prices = _dip_prices()
    # Only 40 tickers → n_xs=40 < 50 every minute → None
    frames = {f"T{i:02d}": _make_frame(prices) for i in range(40)}
    assert session_spreads(frames, "2024-01-02") is None


# ---------------------------------------------------------------------------
# 4. session gate — fewer than 100 eligible minutes → None
# ---------------------------------------------------------------------------
def test_session_gate_min_minutes():
    from research.bflow.xsec_discriminator import session_spreads

    # 60 tickers but short bars: minutes 0..119 + dump window 385..389
    # Eligible minutes in scan [30, 383]: minutes 30..119 = 90 minutes
    # (need finite disp AND finite G; disp needs 30-bar window → t>=29, so 30..119 = 90)
    # 90 < 100 → session skipped → None
    frames = {}
    for i in range(60):
        # dip at 60..90 to create dump, and provide dump window
        rows = []
        for m in range(120):
            p = 95.0 if 60 <= m <= 90 else 100.0
            rows.append(_bar(m, p))
        # add dump window bars 385..389
        for m in range(385, 390):
            rows.append(_bar(m, 100.0))
        frames[f"T{i:02d}"] = pd.DataFrame(rows)

    result = session_spreads(frames, "2024-01-02")
    assert result is None, (
        f"expected None for <100 eligible minutes, got {result}")


# ---------------------------------------------------------------------------
# 5. determinism — frame insertion order doesn't matter
# ---------------------------------------------------------------------------
def test_determinism_frame_order():
    from research.bflow.xsec_discriminator import session_spreads

    prices = _dip_prices()
    tickers = [f"T{i:02d}" for i in range(60)]
    frames_fwd = {t: _make_frame(prices) for t in tickers}
    frames_rev = {t: _make_frame(prices) for t in reversed(tickers)}

    row1 = session_spreads(frames_fwd, "2024-01-02")
    row2 = session_spreads(frames_rev, "2024-01-02")

    assert row1 is not None and row2 is not None
    for key in ("spread_net", "spread_gross", "n_minutes", "mean_n_xs"):
        assert np.isclose(row1[key], row2[key], atol=1e-12), (
            f"determinism failure on key={key}: {row1[key]} vs {row2[key]}")


# ---------------------------------------------------------------------------
# 6. verdict rules — all five outcomes
# ---------------------------------------------------------------------------
def test_verdict_rules():
    from research.bflow.xsec_discriminator import verdict

    assert verdict(5.0, 4.0, 800) == "ECON-PASS"
    assert verdict(5.0, 1.0, 800) == "SIGNAL-ONLY"
    assert verdict(1.0, 0.0, 800) == "NULL"
    assert verdict(-5.0, -4.0, 800) == "INVERTED"
    assert verdict(5.0, 4.0, 500) == "INVALID-DATA"
    assert verdict(float("nan"), float("nan"), 800) == "INVALID-DATA"


# ---------------------------------------------------------------------------
# 7. runner end-to-end
# ---------------------------------------------------------------------------
def _session_df_xs(session_date, n_tickers=60):
    """60-ticker cross-sectional world (dippers+rippers) in one parquet."""
    rows = []
    for i in range(n_tickers // 2):
        dip_val = 95.0 - i * 0.01
        ticker = f"D{i:02d}"
        for m in range(390):
            p = dip_val if 60 <= m <= 200 else 100.0
            rows.append({"ticker": ticker, "minute": m, "o": p,
                         "h": p + 0.2, "l": p - 0.2, "c": p,
                         "v": 1000.0, "vw": p})
    for i in range(n_tickers // 2):
        rip_val = 105.0 + i * 0.01
        ticker = f"R{i:02d}"
        for m in range(390):
            p = rip_val if 60 <= m <= 200 else 100.0
            rows.append({"ticker": ticker, "minute": m, "o": p,
                         "h": p + 0.2, "l": p - 0.2, "c": p,
                         "v": 1000.0, "vw": p})
    return pd.DataFrame(rows)


def test_runner_end_to_end(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    analysis = str(tmp_path / "analysis")

    # 3 sessions × 60 tickers
    for d in ["2024-01-02", "2024-01-03", "2024-01-04"]:
        _session_df_xs(d).to_parquet(cache / f"min_bars_{d}.parquet")

    env = dict(os.environ, PYTHONPATH="src:.")
    proc = subprocess.run(
        [sys.executable, "scripts/run_bflow_phase1e.py",
         "--cache-dir", str(cache), "--analysis-dir", analysis],
        capture_output=True, text=True, env=env, cwd="/root/openclaw")

    assert proc.returncode == 0, proc.stderr[-3000:]

    # INVALID-DATA because n_sessions=3 < 700
    assert "[bflow-p1e] VERDICT: INVALID-DATA" in proc.stdout, (
        f"expected INVALID-DATA verdict in stdout; got:\n{proc.stdout[-1000:]}")

    # report.md + spread_sessions.parquet exist
    report_path = os.path.join(analysis, "bflow_phase1e", "report.md")
    parquet_path = os.path.join(analysis, "bflow_phase1e", "spread_sessions.parquet")
    assert os.path.exists(report_path), "report.md missing"
    assert os.path.exists(parquet_path), "spread_sessions.parquet missing"

    # parquet has 3 rows
    df = pd.read_parquet(parquet_path)
    assert len(df) == 3, f"expected 3 rows in parquet, got {len(df)}"

    # no spread values in per-session progress lines
    # lines that start with "[bflow-p1e] 2024-" (session progress lines)
    progress_lines = [line for line in proc.stdout.splitlines()
                      if line.startswith("[bflow-p1e] 2024-")]
    for line in progress_lines:
        assert "net=" not in line, (
            f"progress line must not print spread values, found 'net=' in: {line!r}")
