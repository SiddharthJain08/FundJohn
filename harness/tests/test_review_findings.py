# harness/tests/test_review_findings.py
"""Regression tests for the two adversarial-review findings.

1. H_max apples-to-apples: run()'s H_max must reach ALL FOUR policies (both
   baselines AND the ensemble) so the design-doc Sec.7 "shared max-hold cap"
   invariant holds for H_max != 30, not just the dormant default.
2. NaN bracket levels must be excluded at extraction (numeric NaN survives the
   IS NOT NULL filter; MEMORY.md's numeric-NaN-vs-NULL poisoning class).
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, "/root/openclaw/src")

import exit_sim as e          # noqa: E402
import inputs as I            # noqa: E402
import run_tdom               # noqa: E402


# --------------------------------------------------------------------------- #
# Finding 1: H_max reaches all four policies
# --------------------------------------------------------------------------- #
def _toy_cluster_inputs():
    """A minimal 2-leg long cluster + the strategies/ctx/ids the orchestrator
    would feed _policies_for_cluster (no DB, fast MC)."""
    legs = [
        dict(strategy_id="s0", stop_loss=95.0, target_1=110.0, target_2=None,
             effective_sharpe=1.0),
        dict(strategy_id="s1", stop_loss=96.0, target_1=108.0, target_2=None,
             effective_sharpe=1.0),
    ]
    cluster = I.Cluster(day="2026-05-04", ticker="TEST", direction=1,
                        entry=100.0, legs=legs, regime="LOW_VOL",
                        easy_to_borrow=True)
    ids = ["s0", "s1"]
    strategies = [e.Strategy(id=s, sharpe=1.0, half_life=10.0, confidence=0.5,
                             direction=1) for s in ids]
    ctx = e.Context(C=np.eye(2), sigma_underlying=2.0, entry_price=100.0,
                    txn_cost=0.0, hurdle_g_star=0.0, kelly_fraction=0.5,
                    session_bars=None)
    regime_tables = {"LOW_VOL": dict(
        weights={"s0": 0.5, "s1": 0.5}, sharpe={"s0": 1.0, "s1": 1.0},
        cadence={"s0": 1.0, "s1": 1.0}, matrix={})}
    cfg = run_tdom.e_config(seed=0, mc_paths=2000, mc_dt=0.5)
    return cluster, strategies, ctx, ids, regime_tables, cfg


def test_hmax_reaches_all_four_policies():
    cluster, strategies, ctx, ids, regime_tables, cfg = _toy_cluster_inputs()
    H = 50
    pols = run_tdom._policies_for_cluster(
        cluster, strategies, ctx, ids, regime_tables, 2.0, cfg, H_max=H)
    assert pols is not None
    # all THREE baselines must ride to the run's H_max, not a hardcoded 30
    for base in ("min_stop_cumulative", "conf_weighted_atr", "current_live_v2"):
        assert pols[base]["time_stop_bars"] == float(H), (
            f"{base} time_stop_bars={pols[base]['time_stop_bars']} != {H}")
    # the ensemble's effective horizon must also be capped at H_max (<=, since
    # the generator may pick a shorter native T_exit)
    assert pols["ensemble"]["time_stop_bars"] <= float(H)


def test_hmax_default_is_noop():
    """At the shipped default H_max=30 the wiring must be byte-equivalent: the
    baselines pin to 30 exactly (the previously-hardcoded value)."""
    cluster, strategies, ctx, ids, regime_tables, cfg = _toy_cluster_inputs()
    pols = run_tdom._policies_for_cluster(
        cluster, strategies, ctx, ids, regime_tables, 2.0, cfg, H_max=30)
    for base in ("min_stop_cumulative", "conf_weighted_atr", "current_live_v2"):
        assert pols[base]["time_stop_bars"] == 30.0


# --------------------------------------------------------------------------- #
# Finding 2: NaN bracket levels excluded at extraction
# --------------------------------------------------------------------------- #
def test_no_nan_bracket_legs_in_extracted_clusters():
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv("/root/openclaw/.env")
    conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        clusters = I.extract_clusters(conn)
    finally:
        conn.close()
    assert clusters, "expected non-empty extraction"
    bad = 0
    for c in clusters:
        if not math.isfinite(float(c.entry)):
            bad += 1
            continue
        for leg in c.legs:
            if not (math.isfinite(float(leg["stop_loss"]))
                    and math.isfinite(float(leg["target_1"]))):
                bad += 1
                break
    assert bad == 0, f"{bad} extracted clusters carry a non-finite bracket level"
