"""Run the SP-6 B-flow Phase-1c ENERGY-model deep-dive: build the pre-enumerated
energy IC grid (M1 + M2) and the symmetric two-leg execution counterfactual (M3),
self-gate on the anchor reproduction, and render the report.

IN-SAMPLE HYPOTHESIS GENERATION ONLY — nothing produced is a result. The binding
spec is ``analysis/bflow_phase1c_energy_grid_preenumeration.md`` (committed
pre-results; the grid/method set may not be expanded or shrunk). Every cell is
pre-enumerated and reported in full; none is selected post-hoc. The dispositive
gate remains the Phase-1b prereg amendment's OOS slice (sessions >= 2026-06-08).

T4 of the deep-dive: the runner + report + real eval. It COMPOSES the T1/T2/T3
build-level functions (``energy_grid`` / ``energy_counterfactual``) with a SINGLE
shared ``pools`` object so the cache is loaded once and so M3's frozen E1 W=15
global-causal beta is byte-identical to the grid's E1 W=15/global cell (both fit
``ef.fit_global_linear`` over ``eg._e1_bundles(pools, 15)`` — sharing ``pools``
guarantees they can never fork). None of the T1/T2/T3 modules are modified.

ARTIFACTS (spec §1 / §2 Output contract; all under ``/root/openclaw/analysis``):
  bflow_phase1c_energy_grid.parquet         (tidy M1+M2 grid; eg.write_grid)
  bflow_phase1c_energy_m3.parquet           (tidy M3 counterfactual; written here
                                             under the spec's M3 name, NOT the T3
                                             module's default counterfactual name)
  bflow_phase1c_energy_report.md            (the full energy report)

THE ANCHOR GATE (the verification step, treated as such):
  The energy grid's unconditional anchors must reproduce the Phase-1b primary t's
  (ofi_15 -> ret_to_dump t ~ -2.81; vwap_disp_30 -> ret_to_dump t ~ -2.15), same
  ``abs(t - ref) <= 0.15`` tolerance the exploratory runner uses. The grid, the
  60-valid-bar floor, the session enumerator and ``_spearman_ic`` are all SHARED
  with the exploratory menu, so these SHOULD match; the gate exists to catch
  loader / pooling drift. If they DIVERGE the script STOPS and writes NO report
  (the parquets are raw data and are written either way) and exits non-zero so
  the divergence surfaces as a defect rather than being papered over.

Usage: nice -n 19 python3 scripts/run_bflow_phase1c_energy.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/.config/superpowers/worktrees/sp6-bflow-phase1-oracle")

from src.research.bflow import energy_counterfactual as ec
from src.research.bflow import energy_grid as eg

DEFAULT_CACHE_DIR = eg.DEFAULT_CACHE_DIR
DEFAULT_ANALYSIS_DIR = eg.DEFAULT_ANALYSIS_DIR
INSAMPLE_END = eg.INSAMPLE_END

REPORT_NAME = "bflow_phase1c_energy_report.md"
M3_PARQUET_NAME = "bflow_phase1c_energy_m3.parquet"   # spec §2 Output contract

# Phase-1b reference primary t's (from the pre-registered Phase-1b run) for the
# anchor sanity gate — IDENTICAL values + tolerance to the exploratory runner.
P1B_REF = {"ofi_15": -2.81, "vwap_disp_30": -2.15}
ANCHOR_TOL = 0.15

HEADER = (
    "IN-SAMPLE EXPLORATORY — HYPOTHESIS GENERATION ONLY. Nothing here is a "
    "result; every cell was pre-enumerated in the binding spec; the grid is "
    "reported in full and none is selected post-hoc. The energy construction is "
    "a hypothesis-generation instrument; the dispositive gate is unchanged — "
    "real-intent OOS sessions >= 2026-06-08 per the Phase-1b prereg amendment "
    "(M1-M3 propose, M4 disposes)."
)


# ==========================================================================
# small formatting helpers
# ==========================================================================
def _fmt(x, nd=3):
    if x is None or (isinstance(x, float) and (np.isnan(x) or not np.isfinite(x))):
        return "n/a"
    return f"{x:.{nd}f}"


def _grid_lookup(grid_df):
    """Map cell_id -> (mean_ic, t, n_sessions, extra) for O(1) report access."""
    out = {}
    for _, r in grid_df.iterrows():
        out[r["cell_id"]] = (r["mean_ic"], r["t"], int(r["n_sessions"]),
                             r.get("extra", ""))
    return out


def _cell(lut, cell_id):
    if cell_id not in lut:
        return ("n/a", "n/a", 0, "")
    m, t, n, extra = lut[cell_id]
    return (_fmt(m, 4), _fmt(t, 2), n, extra)


def _cid(family, variant, target):
    """Mirror ``energy_grid._cell_id`` exactly."""
    return f"{family}:{variant}->{target}"


# ==========================================================================
# anchor gate
# ==========================================================================
def check_anchors(grid_df):
    """Verify the energy grid's unconditional anchors reproduce the Phase-1b
    primary t's. Returns (ok: bool, rows: list of (label, mean_ic, t, ref, ok)).
    The grid anchor labels are ``ofi_15`` and ``vwap_disp_30_1b`` (the 1b
    vwap-displacement object, re-reported for continuity)."""
    lut = _grid_lookup(grid_df)
    checks = [
        ("ofi_15", _cid("ANCHOR", "ofi_15", "ret_to_dump"), P1B_REF["ofi_15"]),
        ("vwap_disp_30", _cid("ANCHOR", "vwap_disp_30_1b", "ret_to_dump"),
         P1B_REF["vwap_disp_30"]),
    ]
    rows = []
    ok_all = True
    for label, cid, ref in checks:
        if cid in lut:
            m, t, _n, _e = lut[cid]
            ok = bool(np.isfinite(t) and abs(t - ref) <= ANCHOR_TOL)
        else:
            m, t, ok = float("nan"), float("nan"), False
        ok_all = ok_all and ok
        rows.append((label, m, t, ref, ok))
    return ok_all, rows


# ==========================================================================
# the report (pure: (grid_df, m3_df, anchor_ok, anchor_rows, n_sessions)->md str)
# ==========================================================================
def _family_rows(L, lut, grid_df, family, header, note, value_col="IC"):
    """Emit a markdown table of every grid row for ``family`` in the grid's own
    deterministic order. Reports mean_ic + t + n_sessions + the extra field."""
    L.append(f"## {header}")
    L.append("")
    if note:
        L.append(note)
        L.append("")
    L.append(f"| variant | target | mean {value_col} | t | n_sessions | extra |")
    L.append("|---|---|---|---|---|---|")
    sub = grid_df[grid_df["family"] == family]
    for _, r in sub.iterrows():
        m, t, n, extra = _cell(lut, r["cell_id"])
        L.append(f"| {r['variant']} | {r['target']} | {m} | {t} | {n} | {extra} |")
    L.append("")


def build_report(grid_df, m3_df, anchor_ok, anchor_rows, n_sessions):
    """Render the full energy report as a markdown string. Pure — no I/O. Mirrors
    the exploratory report's epistemic discipline and the spec's framing."""
    lut = _grid_lookup(grid_df)
    L = []

    L.append(f"# {HEADER}")
    L.append("")
    L.append(
        f"_Generated for SP-6 B-flow Phase 1c ENERGY-model deep-dive. Binding "
        f"spec: `analysis/bflow_phase1c_energy_grid_preenumeration.md`. Universe "
        f"= the Phase-1b Test-A / exploratory in-sample set (cached "
        f"(ticker,session) pairs, 60-valid-bar floor, session <= "
        f"{INSAMPLE_END}). n_sessions scored = {n_sessions}. Statistic (M1, "
        f"primary) = per-session pooled Spearman IC(r -> target), across-session "
        f"clustered t = mean/(sd/sqrt(n_sessions)); the SESSION is the cluster "
        f"(never a pooled SE). Target set (fixed once): "
        f"{', '.join(eg.TARGETS)}._")
    L.append("")
    L.append("**SIGN CONVENTION (fixed, spec §1):** every cell reports "
             "IC(r -> target). The energy / reversion hypothesis <=> NEGATIVE "
             "IC (overshoot r>0 -> negative forward return; undershoot r<0 -> "
             "positive forward return), matching the Phase-1b "
             "reversion-<=>-negative-IC language.")
    L.append("")

    # ---- multiplicity ----
    n_grid = len(grid_df[grid_df["family"].isin(
        ["E1", "E2", "E3", "E4", "E5"])])
    n_anchor = len(grid_df[grid_df["family"] == "ANCHOR"])
    n_diff = len(grid_df[grid_df["family"] == "ANCHOR_DIFF"])
    L.append("## Multiplicity")
    L.append("")
    L.append(f"- Pre-enumerated GRID cells (E1-E5): **{n_grid}** "
             "(E1=12, E2=9, E3=6, E4=9, E5=6).")
    L.append(f"- Anchors: **{n_anchor}** unconditional + **{n_diff}** paired "
             "r-vs-disp difference rows.")
    L.append("- **Effective independent tests: ~2-3.** The residual targets "
             "(ret_fwd_15 / ret_fwd_30 / ret_to_dump) are highly correlated, "
             "as are the disp_W / OFI_W windows and the residual constructions "
             "across families. A nominal |t| >= 2 over ~40 grid cells with ~2-3 "
             "effective tests is NOT evidence of a real edge; the largest |t| "
             "is an order statistic.")
    L.append("")

    # ---- anchor check (the gate) ----
    L.append("## ANCHOR CHECK (gates everything)")
    L.append("")
    L.append("The energy anchors must reproduce the Phase-1b primary t's. If "
             "they diverge, the loader / pooling drifted and NO other cell is "
             "trustworthy (the script refuses to render this report on a failed "
             "anchor gate).")
    L.append("")
    L.append("| anchor | IC->ret_to_dump | t | Phase-1b ref t | match? |")
    L.append("|---|---|---|---|---|")
    for label, m, t, ref, ok in anchor_rows:
        L.append(f"| {label} | {_fmt(m, 4)} | {_fmt(t, 2)} | {ref:.2f} | "
                 f"{'YES' if ok else 'NO'} |")
    L.append("")
    L.append(f"**Anchors reproduced: {'YES' if anchor_ok else 'NO'}** "
             f"(tolerance abs(t-ref) <= {ANCHOR_TOL}, n_sessions={n_sessions}).")
    L.append("")

    # ---- the DECISIVE r-vs-raw-disp comparison (its own prominent section) ----
    L.append("## DECISIVE COMPARISON — does the energy residual beat raw disp?")
    L.append("")
    L.append("The registered anchor comparison (spec §1): per session, the "
             "PAIRED IC difference **IC(E1-primary r) - IC(raw disp_15)** on the "
             "FROZEN W=15 / global-causal-beta residual, with its OWN clustered "
             "t computed on the per-session differences (a paired test, NOT a "
             "difference of two independent across-session means). The residual "
             "REMOVES the OFI component that carried most of the Phase-1b signal "
             "-- if IC(r) ~ IC(disp) (difference ~ 0), the energy construction "
             "adds nothing beyond the vwap-displacement reversion already "
             "measured in Phase-1b. **That is a legitimate, reportable outcome, "
             "not a failure.**")
    L.append("")
    L.append("| comparison | target | mean IC diff (r - disp) | paired t | "
             "n_sessions |")
    L.append("|---|---|---|---|---|")
    diff_sub = grid_df[grid_df["family"] == "ANCHOR_DIFF"]
    for _, r in diff_sub.iterrows():
        m, t, n, _e = _cell(lut, r["cell_id"])
        L.append(f"| {r['variant']} | {r['target']} | {m} | {t} | {n} |")
    L.append("")

    # ---- ANCHORS (unconditional) ----
    _family_rows(
        L, lut, grid_df, "ANCHOR",
        "ANCHORS — unconditional ICs",
        "The energy anchors are **disp_15 + OFI_15** (the spec §1 energy anchor "
        "line: unconditional IC of disp_15 and OFI_15 alone) plus disp_30 / "
        "ofi_30; `vwap_disp_30_1b` is the Phase-1b vwap-displacement object "
        "re-reported for continuity (NOT the energy disp object).")

    # ---- E1 ----
    _family_rows(
        L, lut, grid_df, "E1",
        "E1 — static linear residual  r = disp_W - beta*OFI_W",
        "W in {15, 30}; beta-mode in {global-causal (PRIMARY, fit on sessions "
        "<= 2026-06-02), per-session pooled OLS (LOOKAHEAD diagnostic upper "
        "bound only -- whole-session fit embeds future minutes in r_t)}. "
        "**Read the `_global` rows as the real numbers; the `_persession` rows "
        "are optimistic upper bounds (spec §1 beta-causality rule).** The "
        "`extra` field carries the M2 leave-one-week-out CV of the fitted beta.")

    # ---- E2 ----
    _family_rows(
        L, lut, grid_df, "E2",
        "E2 — propagator / kernel residual  r = realized_cum - (a + amp*M)",
        "M_t = sum_{tau>=0} exp(-tau/lambda) * ofi-flow_{t-tau}, lambda in "
        "{5, 15, 45} min, amplitude fit globally on sessions <= 2026-06-02. "
        "The literal energy-ledger accounting (flow in vs realized move out). "
        "The `extra` field carries the M2 LOWO-CV of the fitted amplitude.")

    # ---- E3 ----
    _family_rows(
        L, lut, grid_df, "E3",
        "E3 — concave impact law  E[disp] ∝ sign(OFI_15)*sqrt(|OFI_15|)",
        "Square-root law vs linear, W=15, global fit. **The `linear` arm IS the "
        "E1 W=15 / global cell, re-reported here (no de-dup; spec §1 says report "
        "both E3 vs E1).** Tests whether the fair-move function must be concave.")

    # ---- E4 ----
    _family_rows(
        L, lut, grid_df, "E4",
        "E4 — magnitude monotonicity  IC by |r| tercile",
        "Per-session |r| TERCILE (3-way: q1_low / q2_mid / q3_high) on the "
        "FROZEN E1 W=15 / global-beta residual. H_energy predicts "
        "monotonically STRONGER (more negative) IC with larger |r| -- 'more "
        "energy, more pressure'. Tercile membership depends on the intercept "
        "reading: canonical = with-intercept (centered fair-mean residual, spec "
        "§0); the no-intercept diagnostic reading lives in each cell's `extra` "
        "(FLAG-both, not a grid cell).")

    # ---- E5 (the discriminator) ----
    _family_rows(
        L, lut, grid_df, "E5",
        "E5 — sign asymmetry (THE DISCRIMINATOR)  IC for r>0 vs r<0",
        "IC(r -> T) separately for r > 0 (overshoot) and r < 0 (undershoot) on "
        "the same FROZEN E1 W=15 / global-beta residual. **This is the "
        "H_energy-vs-H_absorption discriminator.** H_energy (symmetric "
        "reversion-to-fair): NEGATIVE IC in BOTH sign subsets -- overshoot "
        "reverts down, undershoot catches up. H_absorption (undershoot case "
        "only): overshoot reverts but undershoot does NOT (or continues "
        "AGAINST the flow, i.e. NOT negative / flips sign in the "
        "`undershoot_neg` subset). Sign-subset membership is directly "
        "controlled by the intercept reading -- canonical = with-intercept; the "
        "no-intercept reading is in `extra` (FLAG-both).")

    # ---- M3 ----
    _m3_section(L, m3_df)

    # ---- M2 large-gap flags ----
    _m2_section(L, grid_df)

    # ---- definition notes ----
    _definition_notes(L)

    # ---- headline (in-sample, NOT a result) ----
    _headline(L, grid_df, m3_df, lut)

    # ---- epistemic footer ----
    L.append("## Epistemic footer")
    L.append("")
    L.append(f"- This is hypothesis GENERATION, not testing. At n ~ "
             f"{n_sessions} calm-tape sessions with ~2-3 effective independent "
             "tests over the ~40-cell grid, the largest in-sample |t| is an "
             "ORDER STATISTIC, not an edge.")
    L.append("- **M4 (real-intent OOS, sessions >= 2026-06-08) remains the ONLY "
             "dispositive test.** M1 (session-clustered Spearman) + M2 "
             "(leave-one-week-out CV) + M3 (symmetric two-leg counterfactual) "
             "PROPOSE; M4 DISPOSES. M4 is NOT built in this pass.")
    L.append("- **Every per-session-beta (E1 `_persession`) cell is a LOOKAHEAD "
             "diagnostic upper bound** -- a whole-session OLS fit embeds future "
             "minutes in r_t. The global-causal (<= 2026-06-02) cells are the "
             "real numbers (spec §1 beta-causality rule).")
    L.append("- More conditions / knobs = more researcher degrees of freedom = "
             "LOWER robustness at this n. Robustness comes from MORE DATA and "
             "FEWER parameters.")
    L.append("- The grid stays entirely on IC (rank-correlation) objects -- "
             "immune to the order-book direction-drift confound that "
             "discredited the Phase-1b Test-B policy delta. M3 is the only "
             "method that touches a synthetic book, and it is drift-proofed by "
             "session-centering + the both-legs-positive criterion (the sum is "
             "a drift diagnostic only).")
    L.append("- Any cell carried forward is a CANDIDATE for the Phase-1c "
             "pre-registration only; it must then survive leak-proof OOS "
             "accrual (sessions >= 2026-06-08) before it can be called "
             "anything.")
    L.append("")

    return "\n".join(L) + "\n"


def _m3_section(L, m3_df):
    """M3 table: buy / sell legs side-by-side per z, the both-legs-positive
    criterion stated, the sum row labeled drift-diagnostic-only."""
    L.append("## M3 — symmetric two-leg execution counterfactual")
    L.append("")
    L.append("For EVERY cached (ticker, session) pair on a SYNTHETIC book (NOT "
             "our real intents), BOTH a buy and a sell leg are simulated, each "
             "timed by the FROZEN energy signal = E1 W=15 global-causal-beta "
             "residual r. The trigger uses the SESSION-CENTERED running causal z "
             "of r (trailing-only, t-inclusive); buy enters at z <= -z_thr "
             "(undershoot -> upward pressure -> buy into it), sell at z >= "
             "+z_thr (overshoot -> downward pressure -> sell into it); first "
             "crossing only; fill at vw_{t+1}; dump fallback at minute 384; "
             "Phase-1 differential cost. delta_bps > 0 means the timed entry "
             "BEAT the eod dump.")
    L.append("")
    L.append("**PASS CRITERION (spec §2): BOTH legs INDIVIDUALLY "
             "clustered-positive.** A session-level price drift fires one leg "
             "far more than the other and lifts the favoured leg while sinking "
             "the other, so the both-legs-positive bar can NEVER be satisfied by "
             "drift. The buy+sell SUM is reported as the drift-control "
             "DIAGNOSTIC ONLY, never the pass bar.")
    L.append("")
    L.append("| z | buy mean_bps | buy t | sell mean_bps | sell t | "
             "both legs +? | sum_diag mean | sum_diag t | trig rate (buy/sell) "
             "| fallback rate (buy/sell) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for z in sorted(m3_df["z"].unique()):
        zsub = m3_df[m3_df["z"] == z]
        buy = zsub[zsub["leg"] == "buy"]
        sell = zsub[zsub["leg"] == "sell"]
        sdg = zsub[zsub["leg"] == "sum_diagnostic"]
        buy = buy.iloc[0] if not buy.empty else None
        sell = sell.iloc[0] if not sell.empty else None
        sdg = sdg.iloc[0] if not sdg.empty else None
        both_ok = ec.both_legs_positive(zsub)
        bm = _fmt(buy["mean_bps"], 2) if buy is not None else "n/a"
        bt = _fmt(buy["t"], 2) if buy is not None else "n/a"
        sm = _fmt(sell["mean_bps"], 2) if sell is not None else "n/a"
        st = _fmt(sell["t"], 2) if sell is not None else "n/a"
        sgm = _fmt(sdg["mean_bps"], 2) if sdg is not None else "n/a"
        sgt = _fmt(sdg["t"], 2) if sdg is not None else "n/a"
        trig = (f"{_fmt(buy['trigger_rate'], 2)}/"
                f"{_fmt(sell['trigger_rate'], 2)}"
                if (buy is not None and sell is not None) else "n/a")
        fall = (f"{_fmt(buy['fallback_rate'], 2)}/"
                f"{_fmt(sell['fallback_rate'], 2)}"
                if (buy is not None and sell is not None) else "n/a")
        L.append(f"| {z:.1f} | {bm} | {bt} | {sm} | {st} | "
                 f"{'YES' if both_ok else 'NO'} | {sgm} | {sgt} | {trig} | "
                 f"{fall} |")
    L.append("")
    any_pass = any(ec.both_legs_positive(m3_df[m3_df["z"] == z])
                   for z in m3_df["z"].unique())
    L.append(f"**M3 read: {'at least one z passes both-legs-positive' if any_pass else 'NO z passes the both-legs-positive bar'}** "
             "(in-sample, synthetic book -- a research instrument, NOT a "
             "dispositive result; M4 remains the gate).")
    L.append("")


def _m2_section(L, grid_df):
    """Flag E1-global / E2 / E3 cells whose in-sample-vs-CV gap is large."""
    L.append("## M2 — leave-one-week-out CV (overfit diagnostic)")
    L.append("")
    L.append("Every FITTED scalar (E1 beta, E2 amplitude, E3 concave g) carries "
             "its leave-one-ISO-week-out CV in the grid `extra` field: the "
             "in-sample value and the min / mean / max across held-out-week "
             "refits. A large in-sample-vs-CV gap is an overfit warning. The "
             "full per-cell CV is in the `extra` column of the E1/E2/E3 tables "
             "above; the cells with the WIDEST CV spread are flagged here.")
    L.append("")
    L.append("| family | variant | target | in-sample-vs-CV note |")
    L.append("|---|---|---|---|")
    flagged = 0
    for _, r in grid_df.iterrows():
        if r["family"] not in ("E1", "E2", "E3"):
            continue
        extra = str(r.get("extra", ""))
        if "cv_" not in extra:
            continue
        ins, cmin, cmax = _parse_cv_extra(extra)
        if ins is None or cmin is None or cmax is None:
            continue
        spread = cmax - cmin
        scale = max(abs(ins), 1e-9)
        # flag when the held-out spread exceeds the in-sample magnitude
        if spread > scale:
            flagged += 1
            L.append(f"| {r['family']} | {r['variant']} | {r['target']} | "
                     f"in-sample={ins:.4g}; CV spread [{cmin:.4g}, {cmax:.4g}] "
                     f"(spread {spread:.4g} > |in-sample| {abs(ins):.4g}) -- "
                     "**WIDE; treat the fitted scalar as unstable** |")
    if flagged == 0:
        L.append("| - | - | - | No fitted scalar had a held-out CV spread "
                 "exceeding its in-sample magnitude. |")
    L.append("")


def _parse_cv_extra(extra):
    """Recover (insample, cv_min, cv_max) from a ``_cv_extra`` string. Returns
    (None, None, None) if not parseable. The format is
    ``insample_<k>=...; cv_n_weeks=...; cv_<k>_min=...; cv_<k>_mean=...;
    cv_<k>_max=...`` (see energy_grid._cv_extra)."""
    ins = cmin = cmax = None
    for tok in extra.replace(",", ";").split(";"):
        tok = tok.strip()
        if "=" not in tok:
            continue
        key, val = tok.split("=", 1)
        # the first token carries the "M2 LOWO-CV: " prefix glued to the key
        # (e.g. "M2 LOWO-CV: insample_b"), so match on substring not startswith.
        key = key.strip().split()[-1] if key.strip() else ""
        try:
            fv = float(val.strip())
        except ValueError:
            continue
        if key.startswith("insample_"):
            ins = fv
        elif key.endswith("_min"):
            cmin = fv
        elif key.endswith("_max"):
            cmax = fv
    return ins, cmin, cmax


def _definition_notes(L):
    """Record the disp_W / ofi_30 / realized_cum constructions and the documented
    choices (per the standing constraint: spec-silent details flagged, not
    scanned)."""
    L.append("## Definition notes (constructions + documented choices)")
    L.append("")
    L.append("- **disp_W** (spec: 'return over trailing W min'): "
             "disp_W_t = c_t / c_{t-W} - 1 on the close column `c` off "
             "`flow_features._reindex_valid_frame`'s work-frame. NaN unless BOTH "
             "c_t and c_{t-W} are valid (endpoint-validity). DOCUMENTED CHOICE "
             "(a): column `c` (a close-based return), NOT the harness `vw`-based "
             "vwap-displacement object -- disp_W is plainly a return, a "
             "departure from the harness vwap convention. DOCUMENTED CHOICE (b): "
             "endpoint-validity (c_t & c_{t-W} valid) over full-window-validity "
             "-- the literal 'return over trailing W min' reading. disp_W "
             "REPLACES vwap_disp_30 in every energy cell INCLUDING the anchor "
             "(energy anchor = disp_15 + OFI_15; the vwap_disp_30 anchor is "
             "re-reported ONLY for Phase-1b continuity, labeled `_1b`).")
    L.append("- **OFI_W / ofi_30**: `flow_features._ofi(work, dc, k)` reused "
             "verbatim (trailing sign(delta c)*v / sum v, min_periods=k); ofi_30 "
             "is the same object at k=30. Over the matched W-span, OFI_W's "
             "per-minute sign(delta c)*v deltas telescope over exactly "
             "c_t - c_{t-W}, so disp_W and OFI_W are the spec's matched "
             "'realized move vs flow' pair.")
    L.append("- **E2 realized_cum**: realized_cum_t = c_t / c_{first_valid} - 1 "
             "(expanding causal window from the first valid bar; NaN before/at "
             "it). The only fully-pinned E2 leg. DOCUMENTED CHOICE: the E2 "
             "`ofi-flow` increment (spec-undefined) = per-minute signed volume "
             "sign(delta c_t)*v_t; the propagator M is convolved PER TICKER on "
             "this ticker's own increment (the IIR recursion never crosses a "
             "ticker boundary); the amplitude is a single global scalar fit by "
             "pooled OLS of realized_cum on M over the in-sample sessions.")
    L.append("- **INTERCEPT reading (E4/E5)**: canonical = WITH-intercept "
             "(r = disp - (a + b*OFI), the centered fair-mean residual of spec "
             "§0). A constant shift is Spearman-invariant, so E1/E2/E3 and the "
             "anchors are IDENTICAL under either reading -- the intercept only "
             "bites E4's |r| terciles and E5's sign(r) membership. The "
             "no-intercept reading (r + a) is computed with NO refit and its "
             "E4/E5 ICs ride each canonical cell's `extra` field (the spec's "
             "'FLAG both' requirement), adding NO grid cell.")
    L.append("- **M3 running z** (spec-silent detail): trailing-only, "
             "t-INCLUSIVE (r_t is known at decision time t); sample sd (ddof=1); "
             "z is NaN until >= 2 finite trailing r and where the trailing sd is "
             "0, so a degenerate prefix delays the first entry, never fires "
             "spuriously. The intercept is immaterial to M3 (shifting r by a "
             "constant shifts the running mean and leaves z = (r-mu)/sigma "
             "unchanged).")
    L.append("")


def _e5_verdict(e5num):
    """Data-driven E5 reading on ret_to_dump: classify each sign-subset's IC sign
    and emit the correct H_energy-vs-H_absorption verdict from the ACTUAL signs.

    H_energy <=> NEGATIVE IC in BOTH subsets (symmetric reversion-to-fair). The
    spec §0 H_absorption (undershoot case only): the undershoot leg does NOT
    revert / continues against the flow (i.e. undershoot IC is NOT negative)."""
    def _sign(sub):
        s = e5num.get(sub)
        if s is None or s.empty:
            return 0, float("nan"), float("nan")
        m = float(s.iloc[0]["mean_ic"])
        t = float(s.iloc[0]["t"])
        if not np.isfinite(m):
            return 0, m, t
        return (1 if m > 0 else (-1 if m < 0 else 0)), m, t

    os_sign, _osm, _ost = _sign("overshoot_pos")
    us_sign, _usm, _ust = _sign("undershoot_neg")

    over_rev = (os_sign < 0)      # overshoot reverts (negative IC)
    under_rev = (us_sign < 0)     # undershoot reverts (negative IC)

    if over_rev and under_rev:
        return ("BOTH subsets negative -> H_energy-consistent (symmetric "
                "reversion-to-fair); H_absorption rejected. (In-sample only.)")
    if under_rev and not over_rev:
        # the empirically-observed anomaly: reversion is concentrated in the
        # UNDERSHOOT leg; the overshoot leg does NOT pull back.
        return ("ASYMMETRIC -- reversion is concentrated in the UNDERSHOOT leg "
                "(strongly negative IC: undershoots catch up), while the "
                "OVERSHOOT leg is NOT negative (overshoots do not pull back). "
                "H_energy's both-subsets-negative requirement FAILS (the "
                "overshoot leg breaks it). H_absorption is ALSO REJECTED -- it "
                "predicted the UNDERSHOOT leg to be the non-reverting one, but "
                "empirically the undershoot leg is the strongly-reverting one, "
                "so the anomaly sits on the OVERSHOOT side, the reverse of the "
                "registered H_absorption framing. Neither clean hypothesis fits")
    if over_rev and not under_rev:
        # the textbook H_absorption pattern.
        return ("ASYMMETRIC -- overshoot reverts but undershoot does NOT "
                "(undershoot IC non-negative): the H_absorption pattern "
                "(failed-to-move flow absorbed, price continues against the "
                "flow). H_energy's both-subsets-negative requirement FAILS")
    return ("neither subset reverts (both IC non-negative): no reversion in "
            "either sign-subset -- both H_energy and H_absorption's "
            "reversion-half fail")


def _headline(L, grid_df, m3_df, lut):
    """In-sample headline reading (NOT a result): the decisive comparison, E5
    discriminator, E1 primary, M3."""
    L.append("## Headline reading (in-sample, NOT a result)")
    L.append("")

    # decisive comparison
    diff_sub = grid_df[grid_df["family"] == "ANCHOR_DIFF"]
    diff_bits = []
    for _, r in diff_sub.iterrows():
        diff_bits.append(f"{r['target']} diff={_fmt(r['mean_ic'], 4)} "
                         f"(paired t={_fmt(r['t'], 2)})")
    L.append("- **DECISIVE r-vs-raw-disp (the registered comparison):** "
             + "; ".join(diff_bits) + ". A diff ~ 0 means the energy residual "
             "adds nothing beyond the raw vwap-displacement reversion already "
             "measured in Phase-1b -- a legitimate, reportable null.")

    # E1 primary
    e1bits = []
    for w in (15, 30):
        cid = _cid("E1", f"W{w}_global", "ret_to_dump")
        m, t, _n, _e = _cell(lut, cid)
        e1bits.append(f"W{w}/global -> ret_to_dump IC={m} (t={t})")
    L.append("- **E1 PRIMARY (global-causal beta, the real numbers):** "
             + "; ".join(e1bits) + ". Energy/reversion <=> negative IC.")

    # E5 discriminator — data-driven verdict (the registered discriminator;
    # read the ACTUAL signs, never a fixed conditional).
    e5bits = []
    e5num = {}
    for sub in ("overshoot_pos", "undershoot_neg"):
        cid = _cid("E5", sub, "ret_to_dump")
        m, t, _n, _e = _cell(lut, cid)
        e5bits.append(f"{sub} -> ret_to_dump IC={m} (t={t})")
        e5num[sub] = grid_df[(grid_df["family"] == "E5") &
                             (grid_df["variant"] == sub) &
                             (grid_df["target"] == "ret_to_dump")]
    L.append("- **E5 DISCRIMINATOR (H_energy vs H_absorption) "
             + "; ".join(e5bits) + ":** "
             + _e5_verdict(e5num) + " (n~34, calm tape -- the sign PATTERN is "
             "the read, not the magnitudes; any single |t| is an order "
             "statistic). The two ret_fwd targets are reported in the E5 table "
             "above.")

    # M3 best / worst leg cell
    legs = m3_df[m3_df["leg"].isin(["buy", "sell"])].copy()
    if not legs.empty and legs["t"].notna().any():
        best = legs.loc[legs["t"].idxmax()]
        worst = legs.loc[legs["t"].idxmin()]
        L.append(f"- **M3 (synthetic book):** best leg-cell = {best['leg']} "
                 f"@ z={best['z']:.1f} (mean_bps={_fmt(best['mean_bps'], 2)}, "
                 f"t={_fmt(best['t'], 2)}); worst = {worst['leg']} @ "
                 f"z={worst['z']:.1f} (mean_bps={_fmt(worst['mean_bps'], 2)}, "
                 f"t={_fmt(worst['t'], 2)}). PASS needs BOTH legs positive at "
                 "the same z; the sum row is a drift diagnostic only.")
    L.append("")


# ==========================================================================
# driver — compose T2 grid + T3 M3 with a SHARED pools object, self-gate, write
# ==========================================================================
def run(cache_dir=DEFAULT_CACHE_DIR, insample_end=INSAMPLE_END,
        analysis_dir=DEFAULT_ANALYSIS_DIR, progress=True):
    """Full CACHE-ONLY energy pass: build pools ONCE -> grid (M1+M2) ->
    M3 counterfactual (same pools => same frozen beta) -> anchor gate -> report.

    Returns ``(grid_df, m3_df, anchor_ok)``. Writes both parquets unconditionally
    (raw data); writes the report ONLY when the anchor gate passes. On a failed
    gate it prints the divergence and the report is NOT written -- the caller
    treats that as a defect (non-zero exit)."""
    os.makedirs(analysis_dir, exist_ok=True)

    # 1. build the per-session pooled energy frames ONCE (shared by grid + M3).
    pools = eg.build_session_pools(cache_dir, insample_end, progress=progress)
    n_sessions = len(pools)
    if n_sessions == 0:
        raise RuntimeError(
            f"no in-sample sessions found under {cache_dir} "
            f"(insample_end={insample_end}); cannot build the energy grid")

    # 2. grid (M1 + M2). build_grid returns (grid_df, fits); fits are internal.
    grid_df, _fits = eg.build_grid(pools, insample_end=insample_end)
    grid_path = eg.write_grid(grid_df, analysis_dir)
    if progress:
        print(f"[p1c-energy] {n_sessions} sessions; {len(grid_df)} grid rows "
              f"-> {grid_path}", flush=True)

    # 3. M3 counterfactual — reuse the SAME pools so the frozen E1 W=15 global
    #    beta is byte-identical to the grid's E1 W=15/global cell.
    session_bars, dump_prices = ec._load_session_bars(cache_dir, insample_end)
    m3_df = ec.build_counterfactual(pools, session_bars, dump_prices,
                                    insample_end=insample_end)
    m3_path = os.path.join(analysis_dir, M3_PARQUET_NAME)
    m3_df.to_parquet(m3_path, index=False)
    if progress:
        print(f"[p1c-energy-M3] {len(session_bars)} sessions; {len(m3_df)} rows "
              f"-> {m3_path}", flush=True)

    # 4. ANCHOR GATE — the verification step. STOP + no report on divergence.
    anchor_ok, anchor_rows = check_anchors(grid_df)
    if progress:
        for label, m, t, ref, ok in anchor_rows:
            print(f"[p1c-energy] anchor {label}: IC={_fmt(m, 4)} t={_fmt(t, 2)} "
                  f"(ref {ref:.2f}) -> {'MATCH' if ok else 'DIVERGE'}",
                  flush=True)
    if not anchor_ok:
        print("[p1c-energy] ANCHOR GATE FAILED -- loader/pooling drift "
              "suspected. Report NOT written; surfacing as a defect.",
              file=sys.stderr, flush=True)
        return grid_df, m3_df, anchor_ok

    # 5. report (only on a passing anchor gate).
    report = build_report(grid_df, m3_df, anchor_ok, anchor_rows, n_sessions)
    report_path = os.path.join(analysis_dir, REPORT_NAME)
    with open(report_path, "w") as f:
        f.write(report)
    if progress:
        print(f"[p1c-energy] wrote {report_path} "
              f"({report.count(chr(10))} lines)", flush=True)
    return grid_df, m3_df, anchor_ok


def main(argv=None):
    grid_df, m3_df, anchor_ok = run()
    if not anchor_ok:
        return 1
    print("[p1c-energy] DONE: grid + M3 + report written; anchors reproduced.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
