"""Tests for src/research/bflow/run_phase1b.py — the Phase-1b runner.

Two clusters:

  1. verdict() truth-table — the pure, pre-committed §5 decision function. Built
     from small hand-constructable summary dicts so it is trivially unit-testable
     WITHOUT driving any bars. Covers GO (reversion + momentum sign), KILL,
     WEAK (lone-max-t, sign-inconsistent, coherence-fail, KILL-near-miss).

  2. end-to-end smoke — a synthetic tmpdir mini-cache (2 sessions x 3 tickers) +
     a tiny synthetic orders parquet. Asserts the report + both parquets exist
     and the report carries the VERDICT line. NO network, NO DB, NO real
     prices.parquet, NO real /root/openclaw cache.

Spec: docs/superpowers/specs/2026-06-05-sp6-bflow-phase1b-flow-predictability-prereg.md
"""
import math

import numpy as np
import pandas as pd
import pytest

from src.research.bflow import run_phase1b as rp


# --------------------------------------------------------------------------
# verdict() builders — primary_summary: {feature: t}; secondary_summary:
# {feature: {horizon: t}}; testb_summary: {"reversion_across_session_mean": x}
# --------------------------------------------------------------------------
_FEATS = ("ofi_5", "ofi_15", "vwap_disp_30")
_HORIZONS = ("ret_fwd_5", "ret_fwd_15", "ret_fwd_30", "ret_fwd_60")


def _primary(t1, t2, t3):
    return dict(zip(_FEATS, (t1, t2, t3)))


def _secondary(per_feature):
    """per_feature: {feature: (t5, t15, t30, t60)}; missing features -> all NaN."""
    out = {}
    for f in _FEATS:
        ts = per_feature.get(f, (float("nan"),) * 4)
        out[f] = dict(zip(_HORIZONS, ts))
    return out


def _testb(reversion_mean):
    return {"reversion_across_session_mean": reversion_mean}


# ==========================================================================
# verdict() — GO branch
# ==========================================================================
def test_verdict_go_reversion():
    # 2 of 3 features |t|>=3 on primary, both NEGATIVE (reversion); coherence:
    # ofi_5 has |t|>=2 NEGATIVE in >=2 of its 4 secondary horizons.
    primary = _primary(-4.0, -3.5, -0.5)
    secondary = _secondary({
        "ofi_5": (-2.5, -3.0, -0.1, 0.2),     # 2 horizons negative & |t|>=2
        "ofi_15": (-0.5, -0.5, -0.5, -0.5),
    })
    res, reasons = rp.verdict(primary, secondary, _testb(5.0))
    assert res == "GO"
    assert rp.sign_regime(primary) == "reversion"
    # sign_regime surfaced in reasons
    assert any("reversion" in r for r in reasons)


def test_verdict_go_momentum_positive_sign():
    # 2 of 3 features |t|>=3 on primary, both POSITIVE (momentum). Momentum is
    # GO-with-rule-inverted (prereg §3) — a first-class finding, NOT a kill.
    primary = _primary(4.0, 3.5, 0.5)
    secondary = _secondary({
        "ofi_5": (2.5, 3.0, 0.1, -0.2),       # 2 horizons positive & |t|>=2
        "ofi_15": (0.5, 0.5, 0.5, 0.5),
    })
    res, reasons = rp.verdict(primary, secondary, _testb(-1.0))
    assert res == "GO"
    assert rp.sign_regime(primary) == "momentum"
    assert any("momentum" in r for r in reasons)


def test_verdict_go_third_subthreshold_feature_does_not_break_sign():
    # Third feature at |t|=2.5 (NOT a >=3 survivor) with OPPOSITE sign must NOT
    # break the sign-consistency check (which is pinned to the |t|>=3 set).
    primary = _primary(-4.0, -3.5, +2.5)       # +2.5 is NOT a survivor
    secondary = _secondary({
        "ofi_5": (-2.5, -3.0, -0.1, 0.2),
    })
    res, _ = rp.verdict(primary, secondary, _testb(5.0))
    assert res == "GO"


# ==========================================================================
# verdict() — KILL branch
# ==========================================================================
def test_verdict_kill():
    # all primary |t| < 2 AND reversion-variant across-session mean delta <= 0.
    primary = _primary(1.0, -1.5, 0.3)
    secondary = _secondary({})                  # irrelevant for KILL
    res, reasons = rp.verdict(primary, secondary, _testb(-2.0))
    assert res == "KILL"
    assert any("KILL" in r or "kill" in r.lower() for r in reasons)


def test_verdict_kill_exact_zero_testb():
    # reversion mean EXACTLY 0 -> <= 0 -> KILL (boundary inclusive).
    primary = _primary(0.1, -0.2, 1.9)
    res, _ = rp.verdict(primary, _secondary({}), _testb(0.0))
    assert res == "KILL"


def test_verdict_kill_near_miss_positive_testb_is_weak():
    # all primary |t| < 2 BUT reversion mean > 0 -> NOT kill (testb conjunct
    # fails) and not GO -> WEAK.
    primary = _primary(1.0, -1.5, 0.3)
    res, _ = rp.verdict(primary, _secondary({}), _testb(3.0))
    assert res == "WEAK"


# ==========================================================================
# verdict() — WEAK branch (everything else)
# ==========================================================================
def test_verdict_weak_lone_max_t():
    # Exactly ONE feature |t|>=3 -> fails GO conjunct 1 (need >=2); and it is NOT
    # kill (one |t|>=3 breaks "all < 2") -> WEAK. The pre-registered "lone max-t
    # survivor does not GO" case.
    primary = _primary(-4.0, -1.5, -0.5)
    secondary = _secondary({"ofi_5": (-3.0, -3.0, -3.0, -3.0)})
    res, _ = rp.verdict(primary, secondary, _testb(5.0))
    assert res == "WEAK"


def test_verdict_weak_sign_inconsistent_survivors():
    # 2 features |t|>=3 but OPPOSITE signs -> sign-consistency fails -> not GO;
    # the |t|>=3 features break KILL's "all < 2" -> WEAK.
    primary = _primary(-4.0, +3.5, 0.0)
    secondary = _secondary({
        "ofi_5": (-2.5, -3.0, -0.1, 0.2),
        "ofi_15": (3.0, 3.0, 0.1, 0.1),
    })
    res, _ = rp.verdict(primary, secondary, _testb(5.0))
    assert res == "WEAK"


def test_verdict_weak_coherence_fail():
    # 2 features |t|>=3, consistent NEGATIVE sign, but NO survivor has |t|>=2 at
    # >=2 secondary horizons with the shared sign -> coherence fails -> WEAK.
    primary = _primary(-4.0, -3.5, -0.5)
    secondary = _secondary({
        "ofi_5": (-2.5, 0.1, 0.1, 0.1),       # only 1 horizon |t|>=2 (need 2)
        "ofi_15": (0.5, 0.5, 0.5, -1.9),      # 0 horizons |t|>=2
    })
    res, _ = rp.verdict(primary, secondary, _testb(5.0))
    assert res == "WEAK"


def test_verdict_weak_coherence_wrong_sign():
    # 2 negative survivors, but the only feature with >=2 strong secondaries has
    # them POSITIVE (wrong sign vs the negative shared_sign) -> coherence fails.
    primary = _primary(-4.0, -3.5, -0.5)
    secondary = _secondary({
        "ofi_5": (+2.5, +3.0, +0.1, +0.2),    # strong but WRONG sign
    })
    res, _ = rp.verdict(primary, secondary, _testb(5.0))
    assert res == "WEAK"


def test_verdict_nan_t_values_safe():
    # NaN primary t-values: NaN must NOT count as significant (|NaN|>=3 False)
    # and must count as "<2" for the KILL all-<2 conjunct.
    primary = _primary(float("nan"), float("nan"), float("nan"))
    res, _ = rp.verdict(primary, _secondary({}), _testb(-1.0))
    assert res == "KILL"                       # all "< 2" (NaN) + testb <= 0
    # one NaN + one strong survivor -> not all-<2 -> not KILL; only 1 survivor ->
    # not GO -> WEAK.
    primary2 = _primary(-4.0, float("nan"), 1.0)
    res2, _ = rp.verdict(primary2, _secondary({"ofi_5": (-3.0, -3.0, 0.0, 0.0)}),
                         _testb(-1.0))
    assert res2 == "WEAK"


# ==========================================================================
# sign_regime() classifier
# ==========================================================================
def test_sign_regime_reversion_and_momentum():
    assert rp.sign_regime(_primary(-4.0, -3.5, 0.0)) == "reversion"
    assert rp.sign_regime(_primary(4.0, 3.5, 0.0)) == "momentum"


def test_sign_regime_none_when_no_consistent_survivors():
    # no |t|>=3 survivors -> no consistent sign -> None.
    assert rp.sign_regime(_primary(1.0, -1.0, 0.5)) is None
    # inconsistent survivors -> None.
    assert rp.sign_regime(_primary(-4.0, 4.0, 0.0)) is None


# ==========================================================================
# enumerate_cache_sessions — cache-only session discovery
# ==========================================================================
def test_enumerate_cache_sessions(tmp_path):
    for s in ("2026-04-13", "2026-04-14", "2026-04-15"):
        (tmp_path / f"min_bars_{s}.parquet").write_bytes(b"")
    # a non-matching file must be ignored
    (tmp_path / "something_else.parquet").write_bytes(b"")
    sessions = rp.enumerate_cache_sessions(str(tmp_path))
    assert sessions == ["2026-04-13", "2026-04-14", "2026-04-15"]


def test_enumerate_cache_sessions_missing_dir(tmp_path):
    assert rp.enumerate_cache_sessions(str(tmp_path / "nope")) == []


# ==========================================================================
# end-to-end smoke — synthetic mini-cache + tiny orders parquet
# ==========================================================================
def _write_session_parquet(cache_dir, session, tickers, seed0):
    """Write a min_bars_<session>.parquet with `tickers`, each a complete
    390-bar random-walk session (valid bars, dump window present)."""
    import os
    os.makedirs(cache_dir, exist_ok=True)
    rows = []
    for ti, tk in enumerate(tickers):
        rng = np.random.default_rng(seed0 + ti)
        price = 100.0 + np.cumsum(rng.normal(scale=0.1, size=390))
        prev = 100.0
        for m in range(390):
            c = float(price[m])
            o = prev if m > 0 else c - 0.05
            rows.append({
                "ticker": tk, "minute": m,
                "o": o, "h": c + 0.02, "l": c - 0.02, "c": c,
                "v": float(100 + rng.integers(0, 50)), "vw": c,
            })
            prev = c
    df = pd.DataFrame(rows)
    df.to_parquet(cache_dir + f"/min_bars_{session}.parquet", index=False)


def _write_orders_parquet(path, intents):
    """Tiny orders parquet with the columns the runner reads: grain,
    worked_session, ticker, direction, p_eod_dump, exclude_reason."""
    rows = []
    for it in intents:
        rows.append({
            "grain": it.get("grain", "primary"),
            "worked_session": it["worked_session"],
            "ticker": it["ticker"],
            "direction": it["direction"],
            "p_eod_dump": it.get("p_eod_dump", 100.0),
            "exclude_reason": it.get("exclude_reason", None),
        })
    pd.DataFrame(rows).to_parquet(path, index=False)


def test_main_smoke_writes_report_and_two_parquets(tmp_path):
    cache_dir = str(tmp_path / "cache")
    analysis_dir = str(tmp_path / "analysis")
    orders_path = str(tmp_path / "orders.parquet")

    tickers = ["AAA", "BBB", "CCC"]
    _write_session_parquet(cache_dir, "2026-04-13", tickers, seed0=1)
    _write_session_parquet(cache_dir, "2026-04-14", tickers, seed0=10)

    # tiny orders set: a few primary+included intents per session, plus a broker
    # row and an excluded primary row that the grain/exclude filter must drop.
    intents = []
    for s in ("2026-04-13", "2026-04-14"):
        for tk in tickers:
            intents.append({"worked_session": s, "ticker": tk,
                            "direction": "LONG", "p_eod_dump": 100.5})
    intents.append({"worked_session": "2026-04-13", "ticker": "AAA",
                    "direction": "LONG", "grain": "broker", "p_eod_dump": 100.5})
    intents.append({"worked_session": "2026-04-13", "ticker": "BBB",
                    "direction": "LONG", "exclude_reason": "too_few_bars",
                    "p_eod_dump": 100.5})
    _write_orders_parquet(orders_path, intents)

    rc = rp.main([
        "--cache-dir", cache_dir,
        "--orders-path", orders_path,
        "--analysis-dir", analysis_dir,
    ])
    assert rc == 0

    import os
    report = os.path.join(analysis_dir, "bflow_phase1b_report.md")
    ic_grid = os.path.join(analysis_dir, "bflow_phase1b_ic_grid.parquet")
    policy = os.path.join(analysis_dir, "bflow_phase1b_policy.parquet")
    assert os.path.exists(report)
    assert os.path.exists(ic_grid)
    assert os.path.exists(policy)

    text = open(report).read()
    assert "VERDICT" in text
    # the pre-registration framing quote (load-bearing, spec §2/§6)
    assert "null here = KILL at minute scale" in text or \
           "KILL at minute scale" in text

    # ic_grid parquet: sessions x cells
    grid = pd.read_parquet(ic_grid)
    assert len(grid) >= 1

    # policy parquet: per-intent rows incl. the variant column
    pol = pd.read_parquet(policy)
    assert "variant" in pol.columns
    assert set(pol["variant"].unique()) <= {"reversion", "momentum"}
    # only primary+included intents simulated (3 tickers x 2 sessions x 2 variants)
    assert len(pol) == 3 * 2 * 2


def test_main_smoke_limit_caps_sessions(tmp_path):
    cache_dir = str(tmp_path / "cache")
    analysis_dir = str(tmp_path / "analysis")
    orders_path = str(tmp_path / "orders.parquet")

    tickers = ["AAA", "BBB", "CCC"]
    _write_session_parquet(cache_dir, "2026-04-13", tickers, seed0=1)
    _write_session_parquet(cache_dir, "2026-04-14", tickers, seed0=10)

    intents = [{"worked_session": "2026-04-13", "ticker": tk,
                "direction": "LONG", "p_eod_dump": 100.5} for tk in tickers]
    _write_orders_parquet(orders_path, intents)

    rc = rp.main([
        "--cache-dir", cache_dir,
        "--orders-path", orders_path,
        "--analysis-dir", analysis_dir,
        "--limit", "1",
    ])
    assert rc == 0
    grid = pd.read_parquet(analysis_dir + "/bflow_phase1b_ic_grid.parquet")
    # only the first session in the IC grid
    assert list(grid.index) == ["2026-04-13"] or len(grid) == 1
