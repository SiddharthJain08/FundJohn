"""Hermetic tests for the SP-6 B-flow Phase-1c ENERGY runner (T4).

These exercise the PURE pieces of ``scripts/run_bflow_phase1c_energy.py`` — the
anchor gate, the M2 ``extra`` parser, and the markdown report builder — on
SYNTHETIC in-memory grid / M3 frames. They NEVER touch the minute cache (the real
cache run is the eval, not a test) and never write to ``/root/openclaw/analysis``.
"""
from __future__ import annotations

import importlib.util
import os

import numpy as np
import pandas as pd
import pytest

# 2026-07-14: was the sp6-bflow-phase1-oracle worktree path — pruned in the W8
# cleanup, which broke collection. The script has long been merged to the repo.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "run_bflow_phase1c_energy",
    os.path.join(_REPO, "scripts", "run_bflow_phase1c_energy.py"))
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)

from src.research.bflow import energy_counterfactual as ec
from src.research.bflow import energy_grid as eg


# ==========================================================================
# synthetic grid / M3 fixtures (full pre-enumerated shape, plausible numbers)
# ==========================================================================
def _grid_row(cell_id, family, variant, target, mean_ic, t, n, extra=""):
    return {"cell_id": cell_id, "family": family, "variant": variant,
            "target": target, "mean_ic": mean_ic, "t": t,
            "n_sessions": n, "extra": extra}


def _synthetic_grid(ofi15_t=-2.81, vwap_t=-2.15):
    """A full-shape synthetic energy grid (every family populated) with the two
    anchor t's settable so the gate can be exercised on pass and fail."""
    rows = []
    targets = eg.TARGETS

    # E1: 2 windows x 2 modes x 3 targets = 12
    for w in (15, 30):
        for mode in ("global", "persession"):
            for tg in targets:
                cid = eg._cell_id("E1", f"W{w}_{mode}", tg)
                extra = ("M2 LOWO-CV: insample_b=0.5; cv_n_weeks=5; "
                         "cv_b_min=0.1; cv_b_mean=0.5; cv_b_max=0.9"
                         if mode == "global" else
                         "per-session beta = LOOKAHEAD diagnostic upper bound")
                rows.append(_grid_row(cid, "E1", f"W{w}_{mode}", tg,
                                      -0.02, -1.5, 34, extra))
    # E2: 3 lambdas x 3 targets = 9
    for lam in ("5", "15", "45"):
        for tg in targets:
            cid = eg._cell_id("E2", f"lam{lam}", tg)
            extra = ("M2 LOWO-CV: insample_amplitude=0.01; cv_n_weeks=5; "
                     "cv_amplitude_min=-0.5; cv_amplitude_mean=0.0; "
                     "cv_amplitude_max=0.6")
            rows.append(_grid_row(cid, "E2", f"lam{lam}", tg, -0.01, -0.8, 34,
                                  extra))
    # E3: sqrt + linear x 3 targets = 6
    for variant in ("sqrt", "linear"):
        for tg in targets:
            cid = eg._cell_id("E3", variant, tg)
            extra = ("M2 LOWO-CV: insample_g=0.3; cv_n_weeks=5; cv_g_min=0.2; "
                     "cv_g_mean=0.3; cv_g_max=0.4")
            rows.append(_grid_row(cid, "E3", variant, tg, -0.02, -1.4, 34,
                                  extra))
    # E4: 3 terciles x 3 targets = 9
    for label in ("q1_low", "q2_mid", "q3_high"):
        for tg in targets:
            cid = eg._cell_id("E4", label, tg)
            rows.append(_grid_row(cid, "E4", label, tg, -0.03, -1.0, 34,
                                  "canonical=with-intercept; no-intercept "
                                  "reading mean_ic=-0.02, t=-0.9"))
    # E5: overshoot/undershoot x 3 targets = 6
    for label in ("overshoot_pos", "undershoot_neg"):
        for tg in targets:
            cid = eg._cell_id("E5", label, tg)
            rows.append(_grid_row(cid, "E5", label, tg, -0.04, -1.2, 30,
                                  "canonical=with-intercept; no-intercept "
                                  "reading mean_ic=-0.03, t=-1.0"))
    # ANCHORS: 5 features x 3 targets = 15, with the two gate cells set
    anchor_t = {("ofi_15", "ret_to_dump"): ofi15_t,
                ("vwap_disp_30_1b", "ret_to_dump"): vwap_t}
    for feat in ("disp_15", "disp_30", "ofi_15", "ofi_30", "vwap_disp_30_1b"):
        for tg in targets:
            cid = eg._cell_id("ANCHOR", feat, tg)
            t = anchor_t.get((feat, tg), -1.0)
            rows.append(_grid_row(cid, "ANCHOR", feat, tg, -0.05, t, 34,
                                  "unconditional energy anchor"))
    # ANCHOR_DIFF: 3 (paired r-vs-disp)
    for tg in targets:
        cid = eg._cell_id("ANCHOR", "rMINUSdisp_W15", tg)
        rows.append(_grid_row(cid, "ANCHOR_DIFF", "rMINUSdisp_W15", tg,
                              0.001, 0.3, 34,
                              "paired per-session IC(r)-IC(disp) difference"))
    return eg._sort_grid(pd.DataFrame(rows, columns=eg._GRID_COLUMNS))


def _synthetic_m3(buy_t=2.0, sell_t=1.5):
    """A full-shape synthetic M3 frame: 3 z x (buy, sell, sum_diagnostic)."""
    rows = []
    for z in (0.5, 1.0, 1.5):
        rows.append({"z": z, "leg": "buy", "mean_bps": 5.0, "t": buy_t,
                     "n_sessions": 34, "trigger_rate": 0.6,
                     "fallback_rate": 0.4, "extra": "buy leg"})
        rows.append({"z": z, "leg": "sell", "mean_bps": 4.0, "t": sell_t,
                     "n_sessions": 34, "trigger_rate": 0.55,
                     "fallback_rate": 0.45, "extra": "sell leg"})
        rows.append({"z": z, "leg": "sum_diagnostic", "mean_bps": 9.0,
                     "t": 1.0, "n_sessions": 34, "trigger_rate": float("nan"),
                     "fallback_rate": float("nan"), "extra": "sum diag"})
    return pd.DataFrame(rows, columns=ec.OUTPUT_COLUMNS)


# ==========================================================================
# anchor gate
# ==========================================================================
def test_anchor_gate_passes_on_reference_t():
    grid = _synthetic_grid(ofi15_t=-2.81, vwap_t=-2.15)
    ok, rows = runner.check_anchors(grid)
    assert ok is True
    assert len(rows) == 2
    labels = {r[0] for r in rows}
    assert labels == {"ofi_15", "vwap_disp_30"}
    assert all(r[4] for r in rows)


def test_anchor_gate_passes_within_tolerance():
    # within 0.15 of the references
    grid = _synthetic_grid(ofi15_t=-2.81 + 0.14, vwap_t=-2.15 - 0.14)
    ok, _rows = runner.check_anchors(grid)
    assert ok is True


def test_anchor_gate_fails_outside_tolerance():
    grid = _synthetic_grid(ofi15_t=-2.81 + 0.30, vwap_t=-2.15)
    ok, rows = runner.check_anchors(grid)
    assert ok is False
    # the ofi_15 row is the one that diverged
    ofi_row = [r for r in rows if r[0] == "ofi_15"][0]
    assert ofi_row[4] is False


def test_anchor_gate_fails_on_missing_cell():
    grid = _synthetic_grid()
    grid = grid[~((grid["family"] == "ANCHOR") &
                  (grid["variant"] == "ofi_15"))].reset_index(drop=True)
    ok, rows = runner.check_anchors(grid)
    assert ok is False


def test_anchor_gate_fails_on_nan_t():
    grid = _synthetic_grid()
    mask = ((grid["family"] == "ANCHOR") & (grid["variant"] == "ofi_15") &
            (grid["target"] == "ret_to_dump"))
    grid.loc[mask, "t"] = float("nan")
    ok, _rows = runner.check_anchors(grid)
    assert ok is False


# ==========================================================================
# M2 extra parser
# ==========================================================================
def test_parse_cv_extra_recovers_insample_and_bounds():
    extra = ("M2 LOWO-CV: insample_b=0.5; cv_n_weeks=5; cv_b_min=0.1; "
             "cv_b_mean=0.5; cv_b_max=0.9")
    ins, cmin, cmax = runner._parse_cv_extra(extra)
    assert ins == pytest.approx(0.5)
    assert cmin == pytest.approx(0.1)
    assert cmax == pytest.approx(0.9)


def test_parse_cv_extra_handles_amplitude_key():
    extra = ("M2 LOWO-CV: insample_amplitude=0.01; cv_n_weeks=5; "
             "cv_amplitude_min=-0.5; cv_amplitude_mean=0.0; "
             "cv_amplitude_max=0.6")
    ins, cmin, cmax = runner._parse_cv_extra(extra)
    assert ins == pytest.approx(0.01)
    assert cmin == pytest.approx(-0.5)
    assert cmax == pytest.approx(0.6)


def test_parse_cv_extra_unparseable_returns_none():
    ins, cmin, cmax = runner._parse_cv_extra("per-session beta = LOOKAHEAD")
    assert ins is None and cmin is None and cmax is None


# ==========================================================================
# report builder — structure + required epistemic content
# ==========================================================================
def test_report_has_required_sections():
    grid = _synthetic_grid()
    m3 = _synthetic_m3()
    ok, rows = runner.check_anchors(grid)
    md = runner.build_report(grid, m3, ok, rows, n_sessions=34)
    # header / framing
    assert "IN-SAMPLE EXPLORATORY" in md
    assert "HYPOTHESIS GENERATION ONLY" in md
    assert ">= 2026-06-08" in md
    # the decisive comparison gets its own prominent section
    assert "DECISIVE COMPARISON" in md
    assert "rMINUSdisp_W15" in md
    # E5 read as the discriminator
    assert "DISCRIMINATOR" in md
    assert "H_energy" in md and "H_absorption" in md
    # every family present
    for fam_header in ("E1 —", "E2 —", "E3 —", "E4 —", "E5 —", "ANCHORS —"):
        assert fam_header in md
    # M3 both-legs criterion + sum labeled drift-diagnostic
    assert "BOTH legs INDIVIDUALLY" in md
    assert "drift-control DIAGNOSTIC" in md.replace("DIAGNOSTIC ONLY",
                                                    "DIAGNOSTIC")
    # multiplicity + definition notes + epistemic footer
    assert "Effective independent tests" in md
    assert "Definition notes" in md
    assert "Epistemic footer" in md
    # the four epistemic-footer obligations
    assert "calm-tape" in md
    assert "ORDER STATISTIC" in md or "order statistic" in md.lower()
    assert "M4" in md and "dispositive" in md
    assert "LOOKAHEAD" in md


def test_report_lists_every_grid_cell():
    grid = _synthetic_grid()
    m3 = _synthetic_m3()
    ok, rows = runner.check_anchors(grid)
    md = runner.build_report(grid, m3, ok, rows, n_sessions=34)
    # every grid cell_id's (variant,target) must appear in a table row
    for _, r in grid.iterrows():
        # variant strings are unique enough to grep for
        assert r["variant"] in md, f"missing variant {r['variant']}"


def test_report_m3_table_has_all_z_and_both_legs_column():
    grid = _synthetic_grid()
    m3 = _synthetic_m3(buy_t=2.0, sell_t=1.5)
    ok, rows = runner.check_anchors(grid)
    md = runner.build_report(grid, m3, ok, rows, n_sessions=34)
    assert "0.5" in md and "1.0" in md and "1.5" in md
    # both legs positive at every z -> the pass note should fire
    assert "at least one z passes both-legs-positive" in md


def test_report_m3_no_pass_when_a_leg_is_negative():
    grid = _synthetic_grid()
    m3 = _synthetic_m3(buy_t=2.0, sell_t=-1.5)  # sell leg negative everywhere
    ok, rows = runner.check_anchors(grid)
    md = runner.build_report(grid, m3, ok, rows, n_sessions=34)
    assert "NO z passes the both-legs-positive bar" in md


def test_report_flags_wide_cv_spread():
    grid = _synthetic_grid()
    # E2 synthetic extra has cv spread [-0.5, 0.6] = 1.1 >> |insample 0.01|
    m3 = _synthetic_m3()
    ok, rows = runner.check_anchors(grid)
    md = runner.build_report(grid, m3, ok, rows, n_sessions=34)
    assert "WIDE; treat the fitted scalar as unstable" in md


def _e5num(over_ic, under_ic, over_t=1.9, under_t=-3.3):
    """Build the {sub: 1-row DataFrame} shape _e5_verdict expects."""
    def _df(sub, ic, t):
        return pd.DataFrame([{"family": "E5", "variant": sub,
                              "target": "ret_to_dump", "mean_ic": ic, "t": t,
                              "n_sessions": 34}])
    return {"overshoot_pos": _df("overshoot_pos", over_ic, over_t),
            "undershoot_neg": _df("undershoot_neg", under_ic, under_t)}


def test_e5_verdict_observed_asymmetry_overshoot_anomaly():
    # the REAL data: undershoot strongly negative, overshoot positive.
    v = runner._e5_verdict(_e5num(over_ic=+0.037, under_ic=-0.073))
    assert "reversion is concentrated in the UNDERSHOOT leg" in v
    assert "H_energy" in v and "FAILS" in v
    assert "H_absorption is ALSO REJECTED" in v
    assert "OVERSHOOT side" in v


def test_e5_verdict_both_negative_is_h_energy():
    v = runner._e5_verdict(_e5num(over_ic=-0.02, under_ic=-0.05))
    assert "H_energy-consistent" in v
    assert "H_absorption rejected" in v


def test_e5_verdict_textbook_absorption_overshoot_reverts_undershoot_not():
    v = runner._e5_verdict(_e5num(over_ic=-0.04, under_ic=+0.03))
    assert "H_absorption pattern" in v
    assert "H_energy" in v and "FAILS" in v


def test_e5_verdict_neither_reverts():
    v = runner._e5_verdict(_e5num(over_ic=+0.01, under_ic=+0.02))
    assert "neither subset reverts" in v


def test_report_e5_headline_reads_observed_asymmetry():
    # the synthetic grid has overshoot_pos -0.04 / undershoot_neg -0.04 (both
    # negative) -> H_energy-consistent; verify the headline reflects the SIGNS,
    # not a fixed sentence.
    grid = _synthetic_grid()
    m3 = _synthetic_m3()
    ok, rows = runner.check_anchors(grid)
    md = runner.build_report(grid, m3, ok, rows, n_sessions=34)
    assert "H_energy-consistent" in md  # both synthetic E5 ICs are negative


def test_report_is_deterministic():
    grid = _synthetic_grid()
    m3 = _synthetic_m3()
    ok, rows = runner.check_anchors(grid)
    a = runner.build_report(grid, m3, ok, rows, 34)
    b = runner.build_report(grid, m3, ok, rows, 34)
    assert a == b


# ==========================================================================
# constants / contract
# ==========================================================================
def test_m3_parquet_name_matches_spec():
    # the spec §2 Output contract pins the M3 artifact name (NOT the T3 module's
    # default counterfactual filename).
    assert runner.M3_PARQUET_NAME == "bflow_phase1c_energy_m3.parquet"


def test_anchor_reference_matches_exploratory():
    # the gate must use the SAME references + tolerance as the exploratory runner.
    assert runner.P1B_REF["ofi_15"] == pytest.approx(-2.81)
    assert runner.P1B_REF["vwap_disp_30"] == pytest.approx(-2.15)
    assert runner.ANCHOR_TOL == pytest.approx(0.15)
