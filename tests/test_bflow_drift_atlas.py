"""Tests for src/research/bflow/drift_atlas.py — TDD (Phase-1f).

All tests use synthetic tmp caches. NEVER touches real caches (data/cache/min_bars*).
"""
from __future__ import annotations

import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# helpers — mirror test_bflow_xsec_discriminator.py conventions
# ---------------------------------------------------------------------------

def _bar(m, p, v=1000.0):
    return {"minute": m, "o": p, "h": p + 0.2, "l": p - 0.2,
            "c": p, "v": v, "vw": p}


def _make_frame(prices):
    """390-bar DataFrame from a list of 390 prices; vw=o=c=p, h=p+0.2, l=p−0.2."""
    rows = [_bar(m, p) for m, p in enumerate(prices)]
    return pd.DataFrame(rows)


def _flat_prices(p=100.0):
    return [p] * 390


def _linear_prices(p0=100.0, slope=-0.01):
    """Prices p(m) = p0 + slope*m for m ∈ [0, 389]."""
    return [p0 + slope * m for m in range(390)]


# ---------------------------------------------------------------------------
# Test 1: Flat world (60 identical flat tickers)
#   curve_gross all 0, dev all 0, cost_curve == 0 (differential), session eligible
# ---------------------------------------------------------------------------
def test_flat_world():
    """60 identical flat tickers → curve_gross all 0 (valid minutes),
    dev all 0, cost_curve == 0 (differential spread), session eligible."""
    from research.bflow.drift_atlas import session_curves, N_XS_MIN, MIN_VALID_MINUTES

    prices = _flat_prices(100.0)
    frames = {f"T{i:02d}": _make_frame(prices) for i in range(60)}

    row = session_curves(frames, "2024-01-02")
    assert row is not None, "expected eligible session for 60 flat tickers"
    assert row["n_valid_minutes"] >= MIN_VALID_MINUTES, (
        f"expected ≥{MIN_VALID_MINUTES} valid minutes, got {row['n_valid_minutes']}")

    gross = np.array(row["curve_gross"])
    dev = np.array(row["dev"])
    cost = np.array(row["cost_curve"])

    valid = np.isfinite(gross)
    assert valid.sum() >= MIN_VALID_MINUTES, "too few valid minutes"

    # All tickers identical → cross-ticker mean G = constant G of any ticker
    # For flat prices: dump ≈ 100, p(m+1) = 100 → G(m) = (100-100)/100*1e4 = 0
    assert np.all(np.abs(gross[valid]) < 1e-9), (
        f"curve_gross should be 0 for flat tickers; max |val| = {np.abs(gross[valid]).max():.2e}")

    # dev[m] = curve_gross[m] − curve_gross[0]·(389−m)/389 = 0 − 0 = 0
    dev_finite = dev[np.isfinite(dev)]
    assert np.all(np.abs(dev_finite) < 1e-9), (
        f"dev should be 0 for flat world; max |val| = {np.abs(dev_finite).max():.2e}")

    # cost_curve is DIFFERENTIAL: fill-bar spread − dump spread
    # With h=p+0.2, l=p−0.2, vw=p: spread_bps = min(0.4/p*1e4, 50) = constant
    # → fill-bar spread == dump spread → C ≡ 0 at every valid minute
    cost_finite = cost[np.isfinite(cost)]
    assert np.all(np.abs(cost_finite) < 1e-9), (
        f"cost_curve (differential) should be 0 for flat uniform tickers; "
        f"max |val| = {np.abs(cost_finite).max():.2e}")


# ---------------------------------------------------------------------------
# Test 2: Linear-drift world — null-calibration
#   p(m) = 100 − 0.01·m for all tickers (uniform linear drift)
#   Under this path G ∝ (dump_center − fill_time) / dump → the uniform-accrual
#   null is NOT exactly zero (dump window centers at ~387, anchor is 389).
#   The known systematic: dev(m) ≈ G(0)·(−3m/389) (monotone ramp, ~3 bps max).
#   Assert the EXACT ramp shape (mod small floating-point noise).
# ---------------------------------------------------------------------------
def test_linear_drift_null_calibration():
    """Linear price path → dev(m) is a predictable monotone ramp, NOT zero.

    The uniform-accrual null H0(m) = G(0)·(389−m)/389 is anchored at 389, but
    G(m) = (dump − p(m+1))/dump·1e4 with dump ≈ mean(p[385..389]).

    Closed-form derivation:
      G(m) = A − b·(m+1)  where A=(dump−p0)/dump·1e4, b=slope/dump·1e4
      G(0) = A − b
      dev(m) = G(m) − G(0)·(389−m)/389 = m · [G(0)/389 − b]

    This is a monotone ramp of order |b|·m bps (small for slope=−0.01).
    The null-calibration test asserts:
      1. The exact ramp shape holds (matches closed-form within 1e-9).
      2. The maximum |dev| is small (< 5 bps) — linear drift is NEAR-null
         not exactly null; the key finding is the ramp is small and monotone.
    """
    from research.bflow.drift_atlas import session_curves
    from src.research.bflow import oracle

    slope = -0.01
    p0 = 100.0
    prices = _linear_prices(p0, slope)
    # 60 identical tickers
    frames = {f"T{i:02d}": _make_frame(prices) for i in range(60)}

    row = session_curves(frames, "2024-01-02")
    assert row is not None, "expected eligible session"

    gross = np.array(row["curve_gross"])
    dev = np.array(row["dev"])

    # Find anchor G(0)
    anchor_G = gross[0]
    assert np.isfinite(anchor_G), "curve_gross[0] should be finite"

    # Compute dump analytically (bars 385..389, equal volume → simple mean)
    dump_val = np.mean([p0 + slope * m for m in range(385, 390)])
    # Closed-form ramp coefficient: b = slope/dump * 1e4
    b = slope / dump_val * 1e4
    coeff = anchor_G / 389.0 - b  # dev(m) = m * coeff

    valid = np.isfinite(dev)
    assert valid.sum() >= 200, "too few valid dev minutes"

    m_arr = np.arange(389)[valid]
    expected_dev = m_arr * coeff
    actual_dev = dev[valid]

    # Assert exact ramp shape holds within floating-point noise
    max_err = np.abs(actual_dev - expected_dev).max()
    assert max_err < 1e-6, (
        f"dev should match exact ramp m·[G(0)/389−b] within 1e-6 bps; "
        f"max error = {max_err:.2e}")

    # Assert the ramp is small (linear drift is near-null, not perfectly null)
    max_abs_dev = np.abs(actual_dev).max()
    assert max_abs_dev < 5.0, (
        f"dev ramp too large; max |dev| = {max_abs_dev:.4f} bps for slope={slope}")

    # Assert monotone increasing (coeff > 0 for slope < 0)
    assert coeff > 0, f"expected positive ramp coefficient, got {coeff:.6f}"
    assert actual_dev[-1] > actual_dev[0], "dev should increase monotonically"


# ---------------------------------------------------------------------------
# Test 3: Open-spike world → TIMING-STRUCTURE verdict on many sessions
#   Spike prices UP at m ∈ [11, 32] (decision minutes 10..31 fill at bar m+1).
#   Spike raises fill prices above the null path → G dips below H0 →
#   dev(m) < 0 at minutes 10..31, including pre-named TEST_MINUTES 15 and 30
#   (which are adjacent in the tuple). With 800 copies t is very large → TIMING-STRUCTURE.
# ---------------------------------------------------------------------------
def _make_spike_row(session_label, gross_15, gross_30, anchor_G, rng):
    """Build a synthetic session row with a spike at m=15 and m=30.

    Instead of running the full pipeline (too slow for 800 sessions), we
    directly construct the per-session row dict with plausible curves, matching
    the shape that a real spike-at-bars[11,32] world would produce.

    The spike raises fill prices above the null path at m=15 and m=30:
      G(m) dips below the null → dev(m) = G(m) − G(0)·(389−m)/389 < 0.
    """
    curve_gross = np.full(389, np.nan)
    curve_net = np.full(389, np.nan)
    cost_curve = np.full(389, np.nan)
    dev = np.full(389, np.nan)

    # Baseline: linear from anchor_G down to ~0 at m=388
    for m in range(389):
        g = anchor_G * (388 - m) / 388  # linear decay baseline
        curve_gross[m] = g + rng.normal(0, 0.05)
        curve_net[m] = curve_gross[m]
        cost_curve[m] = 0.0

    # Apply spike dip at m=15 and m=30 (fill at bar m+1 is expensive)
    curve_gross[15] += gross_15
    curve_gross[30] += gross_30

    # Recompute dev from scratch using the actual anchor
    actual_anchor = curve_gross[0]
    for m in range(389):
        if np.isfinite(curve_gross[m]) and np.isfinite(actual_anchor):
            dev[m] = curve_gross[m] - actual_anchor * (389 - m) / 389

    return {
        "session": session_label,
        "n_valid_minutes": 389,
        "curve_gross": curve_gross.tolist(),
        "curve_net": curve_net.tolist(),
        "cost_curve": cost_curve.tolist(),
        "dev": dev.tolist(),
        "mean_n_xs": 60.0,
    }


def test_open_spike_timing_structure():
    """Spike at decision minutes 15 and 30 (adjacent in TEST_MINUTES) →
    dev significantly negative at both → TIMING-STRUCTURE verdict.

    Builds rows directly (bypassing slow session_curves) for the verdict test.
    Also verifies the direction of the spike on a single real session.
    """
    from research.bflow.drift_atlas import (
        session_curves, verdict, pooled_stats, MIN_SESSIONS,
    )

    # ---- Part A: single real session — spike creates negative dev ----
    slope = -0.01
    spike_val = 0.5  # add 0.5 to bars 11..32 (fill at m+1 for m=10..31)

    spike_prices = _linear_prices(100.0, slope)[:]
    for m in range(11, 33):
        spike_prices[m] += spike_val

    frames_spike = {f"T{j:02d}": _make_frame(spike_prices) for j in range(60)}
    row = session_curves(frames_spike, "2024-01-02")
    assert row is not None, "expected eligible session with spike prices"

    dev = np.array(row["dev"])
    # Spike at bars 11..32 raises fill prices for decision minutes 10..31
    # → G(m) smaller → dev(m) = G(m) − H0(m) more negative at m=15 and m=30
    assert np.isfinite(dev[15]) and dev[15] < 0, (
        f"expected dev[15] < 0 for spike world; got {dev[15]:.4f}")
    assert np.isfinite(dev[30]) and dev[30] < 0, (
        f"expected dev[30] < 0 for spike world; got {dev[30]:.4f}")

    # ---- Part B: 800 synthetic rows with consistent spike signal → verdict ----
    rng = np.random.default_rng(42)
    anchor_G = 5.0  # typical positive anchor (long positive at open)
    spike_dip = -3.0  # dev contribution at m=15 and m=30

    rows = []
    for i in range(800):
        rows.append(_make_spike_row(
            f"2024-{i:04d}",
            gross_15=spike_dip + rng.normal(0, 0.3),
            gross_30=spike_dip + rng.normal(0, 0.3),
            anchor_G=anchor_G + rng.normal(0, 0.1),
            rng=rng,
        ))

    assert len(rows) >= MIN_SESSIONS, (
        f"need ≥{MIN_SESSIONS} eligible sessions, got {len(rows)}")

    stats = pooled_stats(rows)

    # dev at TEST_MINUTES 15 and 30 should be significantly negative
    dev_mean = stats["dev"]["mean"].to_numpy()
    dev_t = stats["dev"]["t"].to_numpy()
    assert dev_mean[15] < 0, "expected negative mean dev at m=15"
    assert dev_mean[30] < 0, "expected negative mean dev at m=30"
    assert abs(dev_t[15]) >= 3.0, f"expected |t(dev)| ≥ 3 at m=15, got {dev_t[15]:.2f}"
    assert abs(dev_t[30]) >= 3.0, f"expected |t(dev)| ≥ 3 at m=30, got {dev_t[30]:.2f}"

    # 15 and 30 are adjacent in TEST_MINUTES → TIMING-STRUCTURE
    v = verdict(stats["dev"], len(rows))
    assert v == "TIMING-STRUCTURE", (
        f"expected TIMING-STRUCTURE for spike world; got {v}")


# ---------------------------------------------------------------------------
# Test 4: n_xs gate — 40 tickers → every minute invalid → None
# ---------------------------------------------------------------------------
def test_n_xs_gate():
    """40 tickers < N_XS_MIN=50 → session returns None."""
    from research.bflow.drift_atlas import session_curves

    prices = _flat_prices()
    frames = {f"T{i:02d}": _make_frame(prices) for i in range(40)}
    result = session_curves(frames, "2024-01-02")
    assert result is None, (
        f"expected None for 40 tickers (< N_XS_MIN=50), got {result!r}")


# ---------------------------------------------------------------------------
# Test 5: min-minutes gate — short frames (few valid minutes) → None
# ---------------------------------------------------------------------------
def test_min_minutes_gate():
    """Frame with only ~120 bars → <300 valid minutes → None."""
    from research.bflow.drift_atlas import session_curves

    # 60 tickers but only bars 0..119 + dump window 385..389 → few valid G
    frames = {}
    for i in range(60):
        rows = []
        for m in range(120):
            p = 95.0 if 60 <= m <= 90 else 100.0
            rows.append(_bar(m, p))
        # add dump window
        for m in range(385, 390):
            rows.append(_bar(m, 100.0))
        frames[f"T{i:02d}"] = pd.DataFrame(rows)

    result = session_curves(frames, "2024-01-02")
    assert result is None, (
        f"expected None for short frames (<300 valid minutes), got {result!r}")


# ---------------------------------------------------------------------------
# Test 6: verdict rules — INVALID-DATA, adjacency, FLAT, TIMING-STRUCTURE
# ---------------------------------------------------------------------------
def test_verdict_rules():
    """Unit tests for verdict() — all branches without full pipeline."""
    from research.bflow.drift_atlas import verdict, TEST_MINUTES, T_PASS, MIN_SESSIONS

    # Build a minimal dev_stats DataFrame
    def _make_dev_stats(t_at_minutes):
        """DataFrame indexed 0..388 with columns mean/t/n; t=0 everywhere except
        the specified {minute: t_value} overrides."""
        mean_arr = np.zeros(389)
        t_arr = np.zeros(389)
        n_arr = np.full(389, 100)
        for m, tv in t_at_minutes.items():
            t_arr[m] = tv
            mean_arr[m] = tv * 0.1  # sign matches t
        return pd.DataFrame({"mean": mean_arr, "t": t_arr, "n": n_arr})

    # INVALID-DATA: n_sessions < MIN_SESSIONS
    ds = _make_dev_stats({5: 5.0, 15: 5.0})
    assert verdict(ds, MIN_SESSIONS - 1) == "INVALID-DATA"

    # TIMING-STRUCTURE: adjacent pair (5, 15) both |t|≥3, same sign (positive)
    ds = _make_dev_stats({5: 4.0, 15: 4.0})
    assert verdict(ds, MIN_SESSIONS) == "TIMING-STRUCTURE", (
        "adjacent (5,15) both |t|≥3 same sign → TIMING-STRUCTURE")

    # FLAT: non-adjacent pair only — (5 and 30 are not adjacent in TEST_MINUTES)
    ds = _make_dev_stats({5: 4.0, 30: 4.0})
    v = verdict(ds, MIN_SESSIONS)
    assert v == "FLAT", (
        f"non-adjacent significant pair (5,30) → FLAT; got {v}")

    # FLAT: adjacent pair (15,30) both significant but OPPOSITE signs → FLAT
    # (different signs of mean → no same-sign requirement met)
    mean_arr = np.zeros(389)
    t_arr = np.zeros(389)
    n_arr = np.full(389, 100)
    t_arr[15] = 4.0; mean_arr[15] = 0.4    # positive
    t_arr[30] = -4.0; mean_arr[30] = -0.4  # negative
    ds_opp = pd.DataFrame({"mean": mean_arr, "t": t_arr, "n": n_arr})
    assert verdict(ds_opp, MIN_SESSIONS) == "FLAT", (
        "adjacent pair with opposite signs → FLAT")

    # TIMING-STRUCTURE: adjacent pair (300, 330) — last two in TEST_MINUTES
    ds = _make_dev_stats({300: -4.5, 330: -4.5})
    assert verdict(ds, MIN_SESSIONS) == "TIMING-STRUCTURE"

    # Verify adjacency in TEST_MINUTES tuple (sanity)
    tm = TEST_MINUTES
    adjacent_pairs = {(tm[i], tm[i+1]) for i in range(len(tm)-1)}
    assert (5, 15) in adjacent_pairs
    assert (5, 30) not in adjacent_pairs   # not adjacent
    assert (300, 330) in adjacent_pairs


# ---------------------------------------------------------------------------
# Test 7: runner end-to-end — 3-session tmp cache → INVALID-DATA path
#   report.md + curves.parquet exist; no-peek progress lines (no curve values)
# ---------------------------------------------------------------------------
def _session_df_flat(session_date, n_tickers=60, p=100.0):
    """60 identical flat-price tickers for one session."""
    rows = []
    for i in range(n_tickers):
        ticker = f"T{i:02d}"
        for m in range(390):
            rows.append({"ticker": ticker, "minute": m, "o": p,
                         "h": p + 0.2, "l": p - 0.2, "c": p,
                         "v": 1000.0, "vw": p})
    return pd.DataFrame(rows)


def test_runner_end_to_end(tmp_path):
    """Runner on 3-session tmp cache → INVALID-DATA (n<700); artifacts exist;
    no-peek progress lines contain no curve values."""
    cache = tmp_path / "cache"
    cache.mkdir()
    analysis = str(tmp_path / "analysis")

    # 3 sessions × 60 flat tickers
    for d in ["2024-01-02", "2024-01-03", "2024-01-04"]:
        _session_df_flat(d).to_parquet(cache / f"min_bars_{d}.parquet")

    env = dict(os.environ, PYTHONPATH="src:.")
    proc = subprocess.run(
        [sys.executable, "scripts/run_bflow_phase1f.py",
         "--cache-dir", str(cache), "--analysis-dir", analysis],
        capture_output=True, text=True, env=env, cwd="/root/openclaw")

    assert proc.returncode == 0, (
        f"runner failed:\nstdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}")

    # INVALID-DATA because n_sessions=3 < 700
    assert "[bflow-p1f] VERDICT: INVALID-DATA" in proc.stdout, (
        f"expected INVALID-DATA in stdout; got:\n{proc.stdout[-1000:]}")

    # report.md + curves.parquet exist
    report_path = os.path.join(analysis, "bflow_phase1f", "report.md")
    parquet_path = os.path.join(analysis, "bflow_phase1f", "curves.parquet")
    assert os.path.exists(report_path), f"report.md missing at {report_path}"
    assert os.path.exists(parquet_path), f"curves.parquet missing at {parquet_path}"

    # parquet: long format with correct number of rows (3 sessions × 389 minutes)
    df = pd.read_parquet(parquet_path)
    assert len(df) == 3 * 389, (
        f"expected 3×389={3*389} long-format rows, got {len(df)}")
    assert set(df.columns) >= {"session", "minute", "gross", "net", "cost", "dev"}

    # no-peek: per-session progress lines must not contain curve values
    progress_lines = [line for line in proc.stdout.splitlines()
                      if line.startswith("[bflow-p1f] 2024-")]
    assert len(progress_lines) == 3, (
        f"expected 3 session progress lines, got {len(progress_lines)}")
    for line in progress_lines:
        assert "gross=" not in line, f"found 'gross=' in progress line: {line!r}"
        assert "net=" not in line, f"found 'net=' in progress line: {line!r}"
        assert "dev=" not in line, f"found 'dev=' in progress line: {line!r}"
        assert "n_valid_minutes=" in line, (
            f"expected 'n_valid_minutes=' in progress line: {line!r}")


# ---------------------------------------------------------------------------
# Test 8: --from-parquet mode — synthetic curves.parquet → regen report.md
#   5 sessions × 389 minutes; INVALID-DATA at n=5; report contains dev_sys
#   and a VERDICT line.
# ---------------------------------------------------------------------------
def test_from_parquet_mode(tmp_path):
    """--from-parquet skips cache pass; loads long-format curves.parquet;
    regenerates report.md with the fixed writer (dev_sys column + correct note).
    n=5 sessions → INVALID-DATA verdict.
    """
    # Build a 5-session × 389-minute long-format parquet with known values
    rng = np.random.default_rng(99)
    sessions_list = [f"2024-0{i+1}-02" for i in range(5)]
    long_rows = []
    for sess in sessions_list:
        anchor = 5.0 + rng.normal(0, 0.1)
        for m in range(389):
            g = anchor * (389 - m) / 389 + rng.normal(0, 0.05)
            net = g - 0.5
            cost = 0.5
            dev = g - anchor * (389 - m) / 389
            long_rows.append({
                "session": sess,
                "minute": m,
                "gross": g,
                "net": net,
                "cost": cost,
                "dev": dev,
            })

    parquet_path = str(tmp_path / "curves.parquet")
    pd.DataFrame(long_rows).to_parquet(parquet_path, index=False)

    analysis = str(tmp_path / "analysis")

    env = dict(os.environ, PYTHONPATH="src:.")
    proc = subprocess.run(
        [sys.executable, "scripts/run_bflow_phase1f.py",
         "--from-parquet", parquet_path,
         "--analysis-dir", analysis],
        capture_output=True, text=True, env=env, cwd="/root/openclaw")

    assert proc.returncode == 0, (
        f"runner failed:\nstdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}")

    report_path = os.path.join(analysis, "bflow_phase1f", "report.md")
    assert os.path.exists(report_path), f"report.md missing at {report_path}"

    with open(report_path) as fh:
        report_text = fh.read()

    # report must contain the dev_sys column header
    assert "dev_sys" in report_text, (
        f"expected 'dev_sys' in report.md; snippet:\n{report_text[:1000]}")

    # report must contain a VERDICT line (INVALID-DATA at n=5)
    assert "VERDICT:" in report_text, (
        f"expected 'VERDICT:' in report.md; snippet:\n{report_text[:1000]}")
    assert "INVALID-DATA" in report_text, (
        f"expected 'INVALID-DATA' in report.md (n=5 < 700); "
        f"snippet:\n{report_text[:1000]}")
