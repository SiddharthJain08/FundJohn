"""Run the SP-6 B-flow Phase-1c EXPLORATORY conditional-IC menu and render the
report. IN-SAMPLE HYPOTHESIS GENERATION ONLY — nothing produced is a result.

Usage: nice -n 19 python3 scripts/run_bflow_phase1c.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/.config/superpowers/worktrees/sp6-bflow-phase1-oracle")

from src.research.bflow import exploratory_phase1c as p1c
from src.research.bflow import predictability as pr

OUT = "/root/openclaw/analysis/bflow_phase1c_exploratory.md"

HEADER = (
    "IN-SAMPLE EXPLORATORY — HYPOTHESIS GENERATION ONLY. Nothing here is a "
    "result; every cell was pre-enumerated; the menu is reported in full. Any "
    "promising cell is a candidate for the Phase-1c pre-registration and ~4-7 "
    "weeks of OOS accrual away from being testable."
)

# Phase-1b reference primary t's (from the pre-registered Phase-1b run) for the
# anchor sanity gate.
P1B_REF = {"ofi_15": -2.81, "vwap_disp_30": -2.15, "ofi_5": -2.63}


def _fmt(x, nd=3):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    return f"{x:.{nd}f}"


def _row(summ, cell):
    if cell not in summ.index:
        return ("n/a", "n/a", 0)
    r = summ.loc[cell]
    return (_fmt(r["mean_ic"]), _fmt(r["t"], 2), int(r["n_sessions"]))


def main():
    cache_dir = p1c.DEFAULT_CACHE_DIR
    per_session_ics, quad_counts, regime_per_session, sessions = p1c.run_menu(cache_dir)
    grid, summ = p1c._summarize_cells(per_session_ics)
    n_sessions_total = len(per_session_ics)

    # regimes present among the sessions we actually scored
    regimes = [r for r in regime_per_session.values() if r]
    regime_counter = Counter(regimes)
    regimes_present = sorted(regime_counter)
    counts = p1c.count_cells(regimes_present)

    L = []
    L.append(f"# {HEADER}")
    L.append("")
    L.append(f"_Generated for SP-6 B-flow Phase 1c. Universe = Phase-1b Test-A "
             f"(cached (ticker,session) pairs, 60-valid-bar floor, session "
             f"<= {p1c.INSAMPLE_END}). n_sessions scored = {n_sessions_total}. "
             f"Statistic = per-session pooled Spearman IC, across-session "
             f"clustered t = mean/(sd/sqrt(n_sessions)); the SESSION is the "
             f"cluster. Target set (fixed once for the whole menu): "
             f"{', '.join(p1c.TARGETS)}._")
    L.append("")
    L.append("## Menu size (pre-enumerated)")
    L.append("")
    L.append(p1c.MENU_DESCRIPTION)
    L.append("")
    L.append(f"- Base cells (A-E): **{counts['base']}**")
    L.append(f"- Regimes present in-sample: {', '.join(f'{r}={regime_counter[r]}' for r in regimes_present) or 'none'}")
    L.append(f"- F re-slice cells (A+C x {len(regimes_present)} regimes): **{counts['F']}**")
    L.append(f"- **TOTAL MENU CELLS: {counts['total']}**")
    L.append(f"- Effective independent tests: ~2-3 (features ofi_15/vwap_disp_30 "
             f"and the 3 horizons are highly correlated). A nominal |t|>=2 over "
             f"{counts['total']} cells with ~2-3 effective tests is NOT evidence "
             f"of a real edge.")
    L.append("")

    # ---- anchor check ----
    L.append("## ANCHOR CHECK (gates everything)")
    L.append("")
    L.append("Section A's primary-target ICs must reproduce Phase 1b. If they "
             "diverge, the loader/pooling drifted and NO other cell is "
             "trustworthy.")
    L.append("")
    L.append("| feature | A IC->ret_to_dump | A t | Phase-1b ref t | match? |")
    L.append("|---|---|---|---|---|")
    anchor_ok = True
    for feat in ("ofi_15", "vwap_disp_30"):
        cell = f"A:{feat}->ret_to_dump"
        m, t, n = _row(summ, cell)
        ref = P1B_REF.get(feat)
        try:
            tv = summ.loc[cell, "t"]
            ok = abs(tv - ref) <= 0.15
        except Exception:
            tv, ok = float("nan"), False
        anchor_ok = anchor_ok and ok
        L.append(f"| {feat} | {m} | {t} | {ref:.2f} | {'YES' if ok else 'NO'} |")
    L.append("")
    L.append(f"**Anchor reproduced: {'YES' if anchor_ok else 'NO'}** "
             f"(n_sessions={n_sessions_total}).")
    L.append("")

    # ---- Section A ----
    L.append("## A. ANCHOR — unconditional ICs")
    L.append("")
    L.append("| feature | target | mean IC | t | n_sessions |")
    L.append("|---|---|---|---|---|")
    for feat in (p1c.FLOW, p1c.PRICE):
        for t in p1c.TARGETS:
            m, tv, n = _row(summ, f"A:{feat}->{t}")
            L.append(f"| {feat} | {t} | {m} | {tv} | {n} |")
    L.append("")

    # ---- Section B ----
    L.append("## B. RESIDUAL DISPLACEMENT (collinearity-clean)")
    L.append("")
    L.append("On record: *does price displacement carry predictive content "
             "BEYOND what contemporaneous flow explains?* disp_resid = "
             "per-session pooled-OLS residual of vwap_disp_30 on ofi_15. The "
             "raw_vwap_disp_30 row is the same object as A's vwap_disp_30 row.")
    L.append("")
    L.append("| feature | target | mean IC | t | n_sessions |")
    L.append("|---|---|---|---|---|")
    for feat in ("disp_resid", f"raw_{p1c.PRICE}"):
        for t in p1c.TARGETS:
            m, tv, n = _row(summ, f"B:{feat}->{t}")
            L.append(f"| {feat} | {t} | {m} | {tv} | {n} |")
    L.append("")

    # ---- Section C ----
    L.append("## C. CONFIRM CONDITIONING — IC(ofi_15) within sign quadrants")
    L.append("")
    L.append("sign(ofi_15) x sign(vwap_disp_30). Exact-zero feature values "
             "excluded (no sign). Transient-impact theory: the fade should work "
             "in flow-down/price-down (confirmed sell pressure) and NOT in "
             "flow-down/price-up (absorbed selling).")
    L.append("")
    # median per-session n per quadrant
    med_n = {}
    for q, _, _ in p1c._QUADRANTS:
        ns = [c.get(q, 0) for _, c in quad_counts]
        med_n[q] = int(np.median(ns)) if ns else 0
    L.append("| quadrant | target | mean IC | t | n_sessions | median n/session |")
    L.append("|---|---|---|---|---|---|")
    for q, _, _ in p1c._QUADRANTS:
        for t in p1c.TARGETS:
            m, tv, n = _row(summ, f"C:{q}|{p1c.FLOW}->{t}")
            L.append(f"| {q} | {t} | {m} | {tv} | {n} | {med_n[q]} |")
    L.append("")

    # ---- Section D ----
    L.append("## D. INTERACTION — f_conf = ofi_15 * 1{signs agree}")
    L.append("")
    L.append("Compared against plain ofi_15. The disagree/zero block is set to 0 "
             "(compresses rank but is the defined object).")
    L.append("")
    L.append("| feature | target | mean IC | t | n_sessions |")
    L.append("|---|---|---|---|---|")
    for feat in ("f_conf", f"plain_{p1c.FLOW}"):
        for t in p1c.TARGETS:
            m, tv, n = _row(summ, f"D:{feat}->{t}")
            L.append(f"| {feat} | {t} | {m} | {tv} | {n} |")
    L.append("")

    # ---- Section E ----
    L.append("## E. BURST-ONLY — top-tercile |ofi_15| per session")
    L.append("")
    L.append("With (burst_confirmed) and without (burst) the C sign-agreement "
             "condition. Tercile cut is session-relative on pooled |ofi_15|.")
    L.append("")
    L.append("| subset | target | mean IC | t | n_sessions |")
    L.append("|---|---|---|---|---|")
    for feat in ("burst", "burst_confirmed"):
        for t in p1c.TARGETS:
            m, tv, n = _row(summ, f"E:{feat}->{t}")
            L.append(f"| {feat} | {t} | {m} | {tv} | {n} |")
    L.append("")

    # ---- Section F: regime slice ----
    L.append("## F. REGIME SLICE — A and C split by session regime")
    L.append("")
    L.append("Regime source = order_set.session_regime_map (execution_runs). "
             "**Splitting 34 sessions 3 ways is itself a researcher-DOF "
             "expansion** — EVERY regime slice below rests on a small, "
             "noise-dominated n and NONE is interpretable as a finding; the "
             "regime t's are order statistics, not edges. A slice with n<=5 is "
             "flagged inline as uninterpretable, but the caveat applies to all "
             "three. (Example trap: the HIGH_VOL flowDn_priceDn t below looks "
             "large purely because n is tiny.)")
    L.append("")
    for reg in regimes_present:
        reg_sessions = {s for s, r in regime_per_session.items() if r == reg}
        slice_ics = [(s, ics) for s, ics in per_session_ics if s in reg_sessions]
        _g, sreg = p1c._summarize_cells(slice_ics) if slice_ics else (None, pd.DataFrame())
        nrs = len(slice_ics)
        warn = (" — **too few sessions to interpret (any t here is a "
                "small-n order statistic)**" if nrs <= 5 else
                " — small n; not a finding")
        L.append(f"### {reg} (n_sessions = {nrs}){warn}")
        L.append("")
        L.append("| section | cell | mean IC | t | n_sessions |")
        L.append("|---|---|---|---|---|")
        # A: both features x targets
        for feat in (p1c.FLOW, p1c.PRICE):
            for t in p1c.TARGETS:
                cell = f"A:{feat}->{t}"
                m, tv, n = _row(sreg, cell)
                L.append(f"| A | {feat}->{t} | {m} | {tv} | {n} |")
        # C: the two theory-relevant quadrants only kept full; report all 4 x targets
        for q, _, _ in p1c._QUADRANTS:
            for t in p1c.TARGETS:
                cell = f"C:{q}|{p1c.FLOW}->{t}"
                m, tv, n = _row(sreg, cell)
                L.append(f"| C | {q}->{t} | {m} | {tv} | {n} |")
        L.append("")

    # ---- data quality ----
    L.append("## Data quality")
    L.append("")
    L.append(f"- Sessions scored: {n_sessions_total} (in-sample, "
             f"session <= {p1c.INSAMPLE_END}).")
    L.append(f"- Regime coverage: "
             f"{', '.join(f'{r}={regime_counter[r]}' for r in regimes_present) or 'unavailable'}; "
             f"{sum(1 for v in regime_per_session.values() if not v)} sessions "
             f"with no regime label.")
    L.append("")
    L.append("## Headline reading (in-sample, NOT a result)")
    L.append("")
    L.append("- **The joint flow+price model is a NULL in-sample: no conditioned "
             "or interaction cell beats unconditional ofi_15.** The strongest "
             "|t| cells (D f_conf ~-3.3, E burst_confirmed ~-3.0) are within "
             "noise of plain ofi_15 (~-3.2/-2.8) and are order statistics over "
             "90 correlated cells, NOT improvements.")
    L.append("- **B (collinearity-clean):** price displacement carries little "
             "predictive content beyond contemporaneous flow — disp_resid ICs "
             "collapse to |t| <= 1.94 on all targets (from raw 2.15-2.82). The "
             "displacement signal is mostly the flow signal.")
    L.append("- **C (the operator's confirm mechanism):** conditioning ofi_15 "
             "on price confirmation does NOT strengthen reversion in-sample — "
             "the confirmed-sell quadrant (flowDn_priceDn) is WEAKER than "
             "unconditional ofi_15, and the absorbed-selling quadrant flips "
             "positive. This is the direct (negative) in-sample read of the "
             "mechanism, not support for it.")
    L.append("")
    L.append("## Epistemic footer")
    L.append("")
    L.append("- This is hypothesis GENERATION, not testing. At n~"
             f"{n_sessions_total} calm-tape sessions with ~2-3 effective "
             f"independent tests over {counts['total']} menu cells, the largest "
             "in-sample |t| is an order statistic, not an edge.")
    L.append("- More conditions/knobs = more researcher degrees of freedom = "
             "LOWER robustness at this n. Robustness comes from MORE DATA and "
             "FEWER parameters.")
    L.append("- Stays entirely on IC (rank-correlation) objects — immune to the "
             "order-book direction-drift confound that discredited the Phase-1b "
             "Test-B policy delta. NO policy/Δbps number appears anywhere here.")
    L.append("- Any cell carried forward is a CANDIDATE for the Phase-1c "
             "pre-registration only; it must then survive ~4-7 weeks of "
             "leak-proof OOS accrual before it can be called anything.")
    L.append("")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(L) + "\n")
    print(f"[p1c] wrote {OUT} ({len(L)} lines)")
    print(f"[p1c] anchor reproduced: {anchor_ok}; total menu cells: {counts['total']}")
    # echo the summary table to stdout for the operator log
    pd.set_option("display.max_rows", None, "display.width", 200)
    print(summ.round(4).to_string())


if __name__ == "__main__":
    main()
