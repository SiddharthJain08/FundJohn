"""SP-6 B-flow Phase 1c — ENERGY-model IC GRID (M1 scoring layer).

T2 of the Phase-1c ENERGY deep-dive. Binding spec:
``analysis/bflow_phase1c_energy_grid_preenumeration.md`` (committed pre-results;
the grid may not be expanded or shrunk). This module is the SCORING layer: it
consumes T1's construction primitives (``energy_features``) and produces the
PRE-ENUMERATED energy IC grid as session-clustered Spearman ICs (M1, the
primary evaluation method), plus the M2 leave-one-week-out CV scalars reported
BESIDE the in-sample fits.

It computes NO policy / Δbps numbers and imports NO ``flow_policy``: the
symmetric two-leg execution counterfactual (M3) is a SEPARATE downstream task
(spec §2 M3; it is the only method that touches a synthetic book and bps). This
module stays entirely on IC (rank-correlation) objects, which are immune to the
order-book direction-drift confound that discredited the Phase-1b Test-B number.

SIGN CONVENTION (fixed, spec §1): every cell reports IC(r → target); the
energy/reversion hypothesis ⟺ NEGATIVE IC (overshoot r>0 → negative forward
return; undershoot r<0 → positive). The 3 targets are
{ret_fwd_15, ret_fwd_30, ret_to_dump} (spec §1 / exploratory TARGETS).

THE PRE-ENUMERATED GRID (spec §1; reported in full, none selected post-hoc)
--------------------------------------------------------------------------
E1  static linear residual   12 cells = W∈{15,30} × β-mode∈{global-causal
                             PRIMARY, per-session LOOKAHEAD-diag} × 3 targets.
E2  propagator/kernel resid    9 cells = λ∈{5,15,45} × 3 targets (global amp).
E3  concave impact law         6 cells = {sqrt-law, linear(=E1 W15/global,
                             RE-REPORTED — no de-dup, spec §1)} × 3 targets.
E4  magnitude monotonicity     9 cells = per-session |r| TERCILE × 3 targets,
                             on the FROZEN variant E1 W=15/global-β.
E5  sign asymmetry             6 cells = {r>0 overshoot, r<0 undershoot} × 3
                             targets, same frozen variant.
ANCHORS                        unconditional IC of {disp_15, disp_30, ofi_15,
                             ofi_30} (the energy anchor is disp_15+OFI_15, NOT
                             the 1b vwap_disp_30 object) + vwap_disp_30
                             RE-REPORTED for 1b continuity (labeled 1b). PLUS
                             the DECISIVE registered comparison: the paired
                             per-session IC difference (E1-primary r minus raw
                             disp_W) with its own clustered t.
Total = 42 grid cells + anchors, all reported, multiplicity stated.

EVALUATION (spec §2)
--------------------
M1 (PRIMARY, this module): per-session pooled Spearman rank IC, across-session
   clustered t = mean/(sd/√n_sessions). The SESSION is the cluster (never a
   pooled SE). Reuses ``predictability._spearman_ic`` VERBATIM and the
   ``exploratory_phase1c._summarize_cells`` clustered-t math (sample sd ddof=1,
   n<2 → NaN, t = mean/(sd/√n)) over the energy cell-key set — NOT the
   hardwired Phase-1b ``CELLS``/``session_ics``/``ic_grid``.
M2 (this module, beside M1): every fitted scalar's leave-one-ISO-week-out CV
   (``energy_features.cv_global_*``) reported as the in-sample value and the
   min/max/mean across held-out-week refits in the cell ``extra`` field — the
   in-sample-vs-CV gap is the overfit diagnostic. No IC re-evaluation.
M3 (SEPARATE downstream task) and M4 (OOS sessions ≥ 2026-06-08) are NOT in
   this module (grid-scope guard: M4 not built this pass; M3 = the only
   synthetic-book / bps method, kept out per the Output contract).

THE INTERCEPT READING (load-bearing, RESOLVED here — T1 carried it as a flag)
-----------------------------------------------------------------------------
T1 returns BOTH OLS coefficients (a, b) and leaves the with/without-intercept
choice to the consumer. A constant shift is Spearman-invariant, so E1/E2/E3 and
the anchors are IDENTICAL under either reading. It ONLY bites E4 (|r|-tercile
boundaries) and E5 (sign(r) membership). Reporting BOTH readings as primary
cells would make E4=18 / E5=12 — that EXPANDS the grid, which the standing
constraint forbids. So we fix ONE canonical reading and emit the alternative as
a diagnostic in ``extra``:

  CANONICAL = WITH-INTERCEPT (T1's ``apply_*_residual`` default). Rationale
  (spec §0): the hypothesis defines r_t = realized_disp_t − E[disp_t | flow],
  and E[disp | flow] is the FULL conditional mean = a + b·OFI_W. §1's
  "r = disp_W − β·OFI_W" is the shorthand that drops the constant; §0 is the
  conceptual definition and the overshoot/undershoot-vs-the-fair-MEAN test needs
  the centered (with-intercept) residual. The NO-intercept reading
  (r + a, i.e. disp − b·OFI) is computed with NO refit (just add back fit['a'])
  and its E4/E5 ICs land in ``extra`` under the ``no_intercept_*`` keys — this
  satisfies the prereg "FLAG both" without adding a single grid cell.

PURITY: the grid builders are pure (per-session frames / bundles in, tidy
DataFrame out). Cache loading and the parquet write live in a thin ``main()``
mirroring ``run_phase1b`` (CACHE-ONLY; never fetches). The cache + the
``enumerate_sessions`` cutoff are reused VERBATIM from ``exploratory_phase1c``
so the energy universe is the identical 34-session in-sample set.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

from src.research.bflow import energy_features as ef
from src.research.bflow import exploratory_phase1c as p1c
from src.research.bflow import flow_features as ff
from src.research.bflow import oracle
from src.research.bflow import predictability as pr

# ---- fixed target set (spec §1; the exploratory A-named 3-set) --------------
TARGETS = ("ret_fwd_15", "ret_fwd_30", "ret_to_dump")

# ---- grid parameters (frozen by spec §1) ------------------------------------
E1_WINDOWS = (15, 30)
E1_BETA_MODES = (ef.E1_GLOBAL_MODE, ef.E1_PER_SESSION_MODE)
E2_LAMBDAS = ef.KERNEL_LAMBDAS              # (5.0, 15.0, 45.0)
E3_VARIANTS = ("sqrt", "linear")           # linear == E1 W15/global, re-reported
E4_N_TERCILES = 3                          # per-session |r| tercile split
E5_SIGN_SUBSETS = ("overshoot_pos", "undershoot_neg")   # r>0 / r<0
FROZEN_E4E5_WINDOW = 15                    # E4/E5 frozen on E1 W=15/global-β

INSAMPLE_END = ef.INSAMPLE_END             # "2026-06-02"
DEFAULT_CACHE_DIR = p1c.DEFAULT_CACHE_DIR
DEFAULT_ANALYSIS_DIR = "/root/openclaw/analysis"
MIN_VALID_BARS = pr.MIN_VALID_BARS

# Tidy-DataFrame column order (deterministic).
_GRID_COLUMNS = ["cell_id", "family", "variant", "target",
                 "mean_ic", "t", "n_sessions", "extra"]


# ==========================================================================
# 1. per-(ticker, session) ENERGY joint frame  (the _ticker_joint_frame analog)
# ==========================================================================
def _ticker_energy_frame(tdf, dump_price):
    """Minute-indexed frame for ONE (ticker, session) carrying the energy flow
    features (ofi_15, ofi_30), the energy price features (disp_15, disp_30), the
    E2 legs (ofi-flow increment, realized_cum) and the 3 targets — all ROW-
    ALIGNED on the minute axis 0..389. Row-alignment is load-bearing: every
    residual / tercile / sign mask is applied to feature and target jointly off
    this frame, so a residual can never desync from its target.

    Pure — reuses ``energy_features.compute_energy_features`` /
    ``ofi_flow_increment`` / ``realized_cum_return`` and
    ``flow_features.compute_targets`` verbatim (same windows, NaN rules)."""
    feats = ef.compute_energy_features(tdf)        # ofi_5/15/30, disp_15/30
    tgts = ff.compute_targets(tdf, dump_price)     # ret_to_dump, ret_fwd_*
    out = pd.DataFrame(index=feats.index)
    out["ofi_15"] = feats["ofi_15"]
    out["ofi_30"] = feats["ofi_30"]
    out["disp_15"] = feats["disp_15"]
    out["disp_30"] = feats["disp_30"]
    out["vwap_disp_30"] = ff.compute_features(tdf)["vwap_disp_30"]  # 1b anchor
    out["e2_flow_inc"] = ef.ofi_flow_increment(tdf)   # E2 ofi-flow increment
    out["realized_cum"] = ef.realized_cum_return(tdf)  # E2 realized cum return
    # E2 propagator M MUST be convolved PER TICKER on this ticker's own flow
    # increment, BEFORE any cross-ticker pooling. The propagator is a temporal
    # IIR recursion (M_t = flow_t + decay·M_{t-1}); convolving the stacked,
    # concatenated cross-ticker flow would carry one ticker's accumulator across
    # the boundary into the next ticker's M (E1/E3 are immune — their apply is
    # elementwise; only E2 has the recursion). So we precompute one M column per
    # kernel λ here and pool the PRECOMPUTED M downstream.
    for lam in E2_LAMBDAS:
        out[_m_col(lam)] = ef.propagator_convolve(out["e2_flow_inc"], lam)
    for t in TARGETS:
        out[t] = tgts[t]
    return out


def _m_col(lam):
    """Pooled-frame column name for the per-ticker-convolved propagator M at
    kernel ``lam``."""
    return f"e2_M_lam{_lam_tag(lam)}"


def _session_energy_frames(session_bars, dump_prices):
    """List of per-ticker energy joint frames for the qualifying tickers (those
    passing the 60-valid-bar floor), in sorted-ticker order. Mirrors the
    Phase-1b gate-1 filter exactly."""
    frames = []
    for ticker in sorted(session_bars):
        tdf = session_bars[ticker]
        if pr._valid_bar_count(tdf) < MIN_VALID_BARS:
            continue
        frames.append(_ticker_energy_frame(tdf, dump_prices.get(ticker)))
    return frames


def _pool(frames, col):
    """Pool one column across the session's qualifying ticker frames (stacked;
    the minute index repeats across tickers — the intended (ticker, minute)
    pooling). Empty -> empty float Series."""
    if not frames:
        return pd.Series(dtype=float)
    return pd.concat([f[col] for f in frames], ignore_index=True)


def _session_pooled(frames):
    """Pool EVERY energy column across the session's qualifying tickers into a
    single row-aligned frame (one row per (ticker, minute)). This is the
    per-session object the fit-pass and the score-pass both consume."""
    cols = ["ofi_15", "ofi_30", "disp_15", "disp_30", "vwap_disp_30",
            "e2_flow_inc", "realized_cum",
            *[_m_col(lam) for lam in E2_LAMBDAS], *TARGETS]
    if not frames:
        return pd.DataFrame({c: pd.Series(dtype=float) for c in cols})
    return pd.DataFrame({c: _pool(frames, c) for c in cols})


# ==========================================================================
# 2. build the per-session pooled frames over the whole in-sample cache
# ==========================================================================
def build_session_pools(cache_dir=DEFAULT_CACHE_DIR, insample_end=INSAMPLE_END,
                        progress=False):
    """For every in-sample cache session (session <= insample_end) build the
    pooled-across-tickers energy frame. Returns
    ``{session: pooled_DataFrame}`` (sessions with no cache file or zero
    qualifying tickers are skipped). CACHE-ONLY — never fetches."""
    sessions = p1c.enumerate_sessions(cache_dir, insample_end)
    pools = {}
    for session in sessions:
        sdf = p1c._load_session_frame(cache_dir, session)
        if sdf is None or not len(sdf):
            if progress:
                print(f"[p1c-energy] {session}: MISSING -> skipped", flush=True)
            continue
        frames = p1c._ticker_frames(sdf)
        dump = {tk: oracle.dump_benchmark(tdf.to_dict("records"))
                for tk, tdf in frames.items()}
        efr = _session_energy_frames(frames, dump)
        if not efr:
            if progress:
                print(f"[p1c-energy] {session}: 0 qualifying tickers -> skipped",
                      flush=True)
            continue
        pools[session] = _session_pooled(efr)
        if progress:
            print(f"[p1c-energy] {session}: {len(efr)} qualifying tickers pooled",
                  flush=True)
    return pools


# ==========================================================================
# 3. FIT PASS — frozen global scalars (PRIMARY = causal ≤ insample_end)
# ==========================================================================
def _e1_bundles(pools, w):
    """Per-session {'flow': OFI_w, 'price': disp_w} bundles for the E1 fitter at
    window ``w``."""
    return {s: {"flow": p[f"ofi_{w}"], "price": p[f"disp_{w}"]}
            for s, p in pools.items()}


def _e2_bundles(pools, lam):
    """Per-session {'M': per-ticker-convolved propagator M at ``lam``,
    'realized_cum': realized_cum} bundles for the E2 amplitude fit. The M is the
    PRECOMPUTED per-ticker column (NOT re-convolved across the pooled stack), so
    the IIR recursion never crosses a ticker boundary."""
    mc = _m_col(lam)
    return {s: {"M": p[mc], "realized_cum": p["realized_cum"]}
            for s, p in pools.items()}


def _fit_global_amplitude(bundles, insample_end=INSAMPLE_END):
    """GLOBAL amplitude fit for E2 on PRECOMPUTED per-ticker M: pool every
    both-finite (realized_cum, M) pair across the in-sample sessions and fit
    a + amp·M by pooled OLS (with intercept, the uniform convention). Mirrors
    ``energy_features.fit_global_propagator`` EXACTLY except M is taken as-is
    (already per-ticker convolved) instead of re-convolved here. Sessions >
    insample_end filtered out INTERNALLY (causality invariant). Returns
    {'amplitude', 'a', 'n_obs', 'n_sessions'}."""
    sessions = ef._insample_sessions(bundles, insample_end)
    ys, Ms = [], []
    for s in sessions:
        b = bundles[s]
        ys.append(pd.Series(np.asarray(b["realized_cum"], dtype=float))
                  .reset_index(drop=True))
        Ms.append(pd.Series(np.asarray(b["M"], dtype=float)).reset_index(drop=True))
    if ys:
        pooled_y = pd.concat(ys, ignore_index=True)
        pooled_M = pd.concat(Ms, ignore_index=True)
    else:
        pooled_y = pd.Series(dtype=float)
        pooled_M = pd.Series(dtype=float)
    a, amp = ef._ols_fit(pooled_y, pooled_M)
    n_obs = int((pooled_y.notna() & pooled_M.notna()).sum())
    return {"amplitude": amp, "a": a,
            "n_obs": n_obs, "n_sessions": len(sessions)}


def _cv_global_amplitude(bundles, insample_end=INSAMPLE_END):
    """M2 leave-one-ISO-week-out CV of the E2 amplitude on precomputed M, reusing
    ``energy_features._leave_one_week_out`` so the fold structure is byte-
    identical to E1/E3."""
    return ef._leave_one_week_out(
        bundles, insample_end,
        lambda bd: _fit_global_amplitude(bd, insample_end=insample_end))


def _apply_amplitude_residual(realized_cum, M, fit):
    """r = realized_cum − (a + amp·M) on the PRECOMPUTED per-ticker M (no
    re-convolution). NaN propagates where either input is NaN."""
    rc = pd.Series(np.asarray(realized_cum, dtype=float))
    Ms = pd.Series(np.asarray(M, dtype=float))
    Ms.index = rc.index
    amp = fit["amplitude"]
    a = fit.get("a", 0.0)
    if not np.isfinite(amp) or not np.isfinite(a):
        return pd.Series(np.nan, index=rc.index)
    return rc - (a + amp * Ms)


def _e3_bundles(pools):
    """Per-session {'ofi15': OFI_15, 'disp15': disp_15} bundles for the E3
    concave fitter."""
    return {s: {"ofi15": p["ofi_15"], "disp15": p["disp_15"]}
            for s, p in pools.items()}


def fit_all(pools, insample_end=INSAMPLE_END):
    """Run every GLOBAL CAUSAL fit ONCE over the in-sample pools and return the
    frozen scalars + their M2 LOWO-CV (in-sample vs per-held-out-week refits).

    Returns a dict:
      'e1': {w: {'fit': fit, 'cv': cv} for w in E1_WINDOWS}
      'e2': {lam: {'fit': fit, 'cv': cv} for lam in E2_LAMBDAS}
      'e3': {'fit': fit, 'cv': cv}
    The per-session (lookahead) E1 mode is NOT fit here — it is fit per session
    inside the score pass (it has no global scalar by construction)."""
    out = {"e1": {}, "e2": {}, "e3": {}}
    for w in E1_WINDOWS:
        bd = _e1_bundles(pools, w)
        out["e1"][w] = {
            "fit": ef.fit_global_linear(bd, insample_end=insample_end),
            "cv": ef.cv_global_linear(bd, insample_end=insample_end),
        }
    for lam in E2_LAMBDAS:
        bd = _e2_bundles(pools, lam)   # uses the PRECOMPUTED per-ticker M
        out["e2"][lam] = {
            "fit": _fit_global_amplitude(bd, insample_end=insample_end),
            "cv": _cv_global_amplitude(bd, insample_end=insample_end),
        }
    e3bd = _e3_bundles(pools)
    out["e3"] = {
        "fit": ef.fit_global_concave(e3bd, insample_end=insample_end),
        "cv": ef.cv_global_concave(e3bd, insample_end=insample_end),
    }
    return out


def _cv_extra(cv, scalar_key):
    """Summarize an M2 LOWO-CV result for the cell ``extra`` field: the
    in-sample scalar and the min / mean / max of the per-held-out-week refits
    (the in-sample-vs-CV gap is the overfit diagnostic). NaN-safe."""
    ins = float(oracle._f(cv["insample"].get(scalar_key)))
    week_vals = [float(oracle._f(f.get(scalar_key)))
                 for f in cv["cv_by_week"].values()]
    finite = [v for v in week_vals if np.isfinite(v)]
    if finite:
        cmin, cmean, cmax = min(finite), float(np.mean(finite)), max(finite)
    else:
        cmin = cmean = cmax = float("nan")
    return (f"insample_{scalar_key}={ins:.6g}; "
            f"cv_n_weeks={len(cv['cv_by_week'])}; "
            f"cv_{scalar_key}_min={cmin:.6g}; cv_{scalar_key}_mean={cmean:.6g}; "
            f"cv_{scalar_key}_max={cmax:.6g}")


# ==========================================================================
# 4. RESIDUAL builders per session (frozen scalars applied causally)
# ==========================================================================
def _e1_residual(pool, w, mode, fits, with_intercept=True):
    """E1 residual r for one session pool at window ``w``.

    mode == E1_GLOBAL_MODE: apply the FROZEN global (a, b). with_intercept=True
        gives r = disp − (a + b·OFI) (canonical); with_intercept=False gives the
        no-intercept reading r = disp − b·OFI = (with-int r) + a (no refit).
    mode == E1_PER_SESSION_MODE: the LOOKAHEAD diagnostic — fit (a, b) on THIS
        session and residualize (always with-intercept; the no-intercept reading
        is not meaningful for the per-session diagnostic and is never scored)."""
    price = pool[f"disp_{w}"]
    flow = pool[f"ofi_{w}"]
    if mode == ef.E1_PER_SESSION_MODE:
        return ef.e1_residual_per_session(price, flow)
    fit = fits["e1"][w]["fit"]
    r = ef.apply_linear_residual(price, flow, fit)
    if not with_intercept:
        a = fit["a"]
        if np.isfinite(a):
            r = r + a       # r_no_intercept = disp − b·OFI
    return r


def _e2_residual(pool, lam, fits):
    """E2 propagator residual r = realized_cum − (a + amp·M) using the FROZEN
    global amplitude at kernel ``lam``. M is the PRECOMPUTED per-ticker column
    (the pool already carries the per-ticker-convolved M; the recursion never
    crossed a ticker boundary)."""
    fit = fits["e2"][lam]["fit"]
    return _apply_amplitude_residual(pool["realized_cum"], pool[_m_col(lam)], fit)


def _e3_residual(pool, fits):
    """E3 concave residual r = disp_15 − (a + g·sqrt-term) using the FROZEN g."""
    fit = fits["e3"]["fit"]
    return ef.apply_concave_residual(pool["disp_15"], pool["ofi_15"], fit)


def _frozen_e4e5_residual(pool, fits, with_intercept=True):
    """The E1 W=15/global-β residual that E4 and E5 are FROZEN on (spec §1).
    Default = with-intercept (canonical, see module docstring)."""
    return _e1_residual(pool, FROZEN_E4E5_WINDOW, ef.E1_GLOBAL_MODE, fits,
                        with_intercept=with_intercept)


# ==========================================================================
# 5. per-session cell-IC builders
# ==========================================================================
def _ic(feature, target):
    """``predictability._spearman_ic`` verbatim — the single IC primitive."""
    return pr._spearman_ic(feature, target)


def _e1_session_ics(pool, fits):
    """E1: 12 cells per session = W∈{15,30} × mode∈{global,per_session} × T."""
    out = {}
    for w in E1_WINDOWS:
        for mode in E1_BETA_MODES:
            r = _e1_residual(pool, w, mode, fits, with_intercept=True)
            for t in TARGETS:
                out[_cell_id("E1", f"W{w}_{_mode_tag(mode)}", t)] = _ic(r, pool[t])
    return out


def _e2_session_ics(pool, fits):
    """E2: 9 cells per session = λ∈{5,15,45} × T."""
    out = {}
    for lam in E2_LAMBDAS:
        r = _e2_residual(pool, lam, fits)
        for t in TARGETS:
            out[_cell_id("E2", f"lam{_lam_tag(lam)}", t)] = _ic(r, pool[t])
    return out


def _e3_session_ics(pool, fits):
    """E3: 6 cells per session = {sqrt, linear(=E1 W15/global re-reported)} × T.
    No de-dup of the linear arm against E1 (spec §1: report both)."""
    out = {}
    r_sqrt = _e3_residual(pool, fits)
    r_lin = _e1_residual(pool, 15, ef.E1_GLOBAL_MODE, fits, with_intercept=True)
    for t in TARGETS:
        out[_cell_id("E3", "sqrt", t)] = _ic(r_sqrt, pool[t])
        out[_cell_id("E3", "linear", t)] = _ic(r_lin, pool[t])
    return out


def _e4_session_ics(pool, fits, with_intercept=True):
    """E4: 9 cells per session = per-session |r| TERCILE × T, on the FROZEN E1
    W=15/global-β residual. SESSION-RELATIVE: the tercile cuts are computed on
    the within-session pooled |r| (mirrors ``_ic_burst``), so a cell is NaN when
    the session has too few finite r to form 3 terciles. ``with_intercept``
    selects the canonical (True) or the no-intercept diagnostic (False)."""
    r = _frozen_e4e5_residual(pool, fits, with_intercept=with_intercept)
    out = {}
    absr = r.abs()
    finite = absr.dropna()
    masks = _tercile_masks(absr, finite)   # low / mid / high (None each if N/A)
    tag = ("" if with_intercept else "noint_")
    for label, mask in masks.items():
        for t in TARGETS:
            cid = _cell_id("E4", f"{tag}{label}", t)
            if mask is None:
                out[cid] = float("nan")
            else:
                out[cid] = _ic(r.where(mask), pool[t].where(mask))
    return out


def _e5_session_ics(pool, fits, with_intercept=True):
    """E5: 6 cells per session = {r>0 overshoot, r<0 undershoot} × T, on the
    same FROZEN E1 W=15/global-β residual. Membership is DIRECTLY controlled by
    the intercept reading — canonical = with-intercept (the centered fair-mean
    residual, spec §0)."""
    r = _frozen_e4e5_residual(pool, fits, with_intercept=with_intercept)
    out = {}
    pos = r > 0
    neg = r < 0
    tag = ("" if with_intercept else "noint_")
    for label, mask in (("overshoot_pos", pos), ("undershoot_neg", neg)):
        for t in TARGETS:
            out[_cell_id("E5", f"{tag}{label}", t)] = _ic(
                r.where(mask), pool[t].where(mask))
    return out


def _anchor_session_ics(pool):
    """Unconditional anchor ICs per session: disp_15, disp_30, ofi_15, ofi_30
    (the energy anchors) + vwap_disp_30 (re-reported for 1b continuity)."""
    out = {}
    for feat, label in (("disp_15", "disp_15"), ("disp_30", "disp_30"),
                        ("ofi_15", "ofi_15"), ("ofi_30", "ofi_30"),
                        ("vwap_disp_30", "vwap_disp_30_1b")):
        for t in TARGETS:
            out[_cell_id("ANCHOR", label, t)] = _ic(pool[feat], pool[t])
    return out


def _r_vs_disp_session_diffs(pool, fits):
    """The DECISIVE registered comparison (spec §1 anchor): per session, the
    PAIRED IC difference (E1-primary r minus raw disp_W) for the FROZEN W=15
    on each target. Returns {cell_id: per_session_diff}. The clustered t is then
    computed on these per-session DIFFERENCES (a paired test), NOT as a
    difference of two independent across-session means."""
    w = FROZEN_E4E5_WINDOW
    r = _e1_residual(pool, w, ef.E1_GLOBAL_MODE, fits, with_intercept=True)
    disp = pool[f"disp_{w}"]
    out = {}
    for t in TARGETS:
        ic_r = _ic(r, pool[t])
        ic_d = _ic(disp, pool[t])
        if np.isfinite(ic_r) and np.isfinite(ic_d):
            out[_cell_id("ANCHOR", f"rMINUSdisp_W{w}", t)] = ic_r - ic_d
        else:
            out[_cell_id("ANCHOR", f"rMINUSdisp_W{w}", t)] = float("nan")
    return out


# ==========================================================================
# 6. cell-id / tag helpers (deterministic naming)
# ==========================================================================
def _mode_tag(mode):
    return "global" if mode == ef.E1_GLOBAL_MODE else "persession"


def _lam_tag(lam):
    """Stable integer-ish tag for a kernel lambda (5.0 -> '5')."""
    return f"{int(lam)}" if float(lam).is_integer() else f"{lam}"


def _cell_id(family, variant, target):
    """Deterministic cell id ``family:variant->target`` (the '|'/'->' idioms the
    exploratory module already uses)."""
    return f"{family}:{variant}->{target}"


def _parse_cell_id(cell_id):
    """(family, variant, target) from a cell id."""
    fam, rest = cell_id.split(":", 1)
    variant, target = rest.split("->", 1)
    return fam, variant, target


# ==========================================================================
# 7. per-session tercile masks (session-relative; mirrors _ic_burst)
# ==========================================================================
def _tercile_masks(absr, finite):
    """Low / mid / high |r| terciles for ONE session pool (session-relative
    cuts on the within-session pooled |r|). Returns {label: boolean Series or
    None}; None when the session has < E4_N_TERCILES finite r (no defined
    terciles -> the cell is NaN that session, dropped from n_sessions exactly
    like a zero-variance IC). Exact-tie handling matches pandas quantile."""
    if len(finite) < E4_N_TERCILES:
        return {"q1_low": None, "q2_mid": None, "q3_high": None}
    lo_cut = finite.quantile(1.0 / E4_N_TERCILES)        # ~33rd pct
    hi_cut = finite.quantile(2.0 / E4_N_TERCILES)        # ~67th pct
    low = absr <= lo_cut
    high = absr > hi_cut
    mid = (absr > lo_cut) & (absr <= hi_cut)
    # NaN absr -> all three masks False there (it carries no tercile).
    return {"q1_low": low.fillna(False),
            "q2_mid": mid.fillna(False),
            "q3_high": high.fillna(False)}


# ==========================================================================
# 8. whole-session driver -> per-session cell ICs (+ the paired diffs)
# ==========================================================================
def session_energy_cells(pool, fits):
    """All energy cell ICs for ONE session pool (E1,E2,E3,E4,E5,anchors) plus
    the diagnostic no-intercept E4/E5 ICs and the paired r-vs-disp differences.
    Returns (ics_dict, diffs_dict). ``ics_dict`` carries every grid cell + the
    ``noint_`` diagnostic E4/E5; ``diffs_dict`` carries the paired per-session
    differences (summarized separately as a paired test)."""
    ics = {}
    ics.update(_e1_session_ics(pool, fits))
    ics.update(_e2_session_ics(pool, fits))
    ics.update(_e3_session_ics(pool, fits))
    ics.update(_e4_session_ics(pool, fits, with_intercept=True))
    ics.update(_e5_session_ics(pool, fits, with_intercept=True))
    # no-intercept diagnostic readings of E4/E5 (the "FLAG both" requirement,
    # carried in extra — NOT new grid cells).
    ics.update(_e4_session_ics(pool, fits, with_intercept=False))
    ics.update(_e5_session_ics(pool, fits, with_intercept=False))
    ics.update(_anchor_session_ics(pool))
    diffs = _r_vs_disp_session_diffs(pool, fits)
    return ics, diffs


# ==========================================================================
# 9. across-session clustered-t summary  (M1)
# ==========================================================================
def _summarize(per_session_cells):
    """Reuse the ``exploratory_phase1c._summarize_cells`` clustered-t math
    (sample sd ddof=1, n<2 -> NaN, t = mean/(sd/√n)) over the energy cell set.
    Returns (grid_sessions×cells, summary_index_by_cell)."""
    return p1c._summarize_cells(per_session_cells)


# ==========================================================================
# 10. tidy grid assembly
# ==========================================================================
def _is_diagnostic_cell(cell_id):
    """The no-intercept E4/E5 readings are DIAGNOSTICS (FLAG-both), not grid
    cells — they ride the canonical cell's ``extra`` field, never their own
    grid row."""
    _, variant, _ = _parse_cell_id(cell_id)
    return variant.startswith("noint_")


def _noint_counterpart(cell_id):
    """Map a canonical E4/E5 cell id to its no-intercept diagnostic id."""
    fam, variant, target = _parse_cell_id(cell_id)
    return _cell_id(fam, f"noint_{variant}", target)


def build_grid(pools, insample_end=INSAMPLE_END):
    """Build the full tidy energy IC grid. Returns
    ``(grid_df, fits)`` where grid_df has columns
    ``cell_id, family, variant, target, mean_ic, t, n_sessions, extra`` in a
    deterministic (family, variant, target) order, one row per pre-enumerated
    cell + the anchors + the paired r-vs-disp difference rows.

    M2 CV summaries land in ``extra`` on the relevant fitted-scalar cells; the
    no-intercept E4/E5 ICs land in ``extra`` on their canonical cells. No grid
    row is ever added for a diagnostic — the grid membership is exactly the
    pre-enumerated set."""
    fits = fit_all(pools, insample_end=insample_end)

    per_session_cells = []
    per_session_diffs = []
    for session in sorted(pools):
        ics, diffs = session_energy_cells(pools[session], fits)
        per_session_cells.append((session, ics))
        per_session_diffs.append((session, diffs))

    _grid, summ = _summarize(per_session_cells)
    _dgrid, dsumm = _summarize(per_session_diffs)

    # Canonical grid cells (drop the no-intercept diagnostics from the rows).
    rows = []
    for cell_id in summ.index:
        if _is_diagnostic_cell(cell_id):
            continue
        fam, variant, target = _parse_cell_id(cell_id)
        rows.append(_grid_row(cell_id, fam, variant, target, summ, dsumm,
                              fits, per_session_cells))
    # Paired r-vs-disp difference rows (their own clustered t).
    for cell_id in dsumm.index:
        fam, variant, target = _parse_cell_id(cell_id)
        rows.append(_diff_row(cell_id, fam, variant, target, dsumm))

    grid_df = pd.DataFrame(rows, columns=_GRID_COLUMNS)
    grid_df = _sort_grid(grid_df)
    return grid_df, fits


def _grid_row(cell_id, fam, variant, target, summ, dsumm, fits,
              per_session_cells):
    """One canonical grid row, with the family-appropriate ``extra`` content
    (M2 CV for the fitted families; the no-intercept IC for E4/E5)."""
    mean_ic = float(summ.loc[cell_id, "mean_ic"])
    t = float(summ.loc[cell_id, "t"])
    n = int(summ.loc[cell_id, "n_sessions"])
    extra = _extra_for(cell_id, fam, variant, target, fits, summ)
    return {"cell_id": cell_id, "family": fam, "variant": variant,
            "target": target, "mean_ic": mean_ic, "t": t, "n_sessions": n,
            "extra": extra}


def _diff_row(cell_id, fam, variant, target, dsumm):
    """A paired r-vs-disp difference row: mean of the per-session IC differences
    with its OWN clustered t (the paired test)."""
    mean_ic = float(dsumm.loc[cell_id, "mean_ic"])
    t = float(dsumm.loc[cell_id, "t"])
    n = int(dsumm.loc[cell_id, "n_sessions"])
    return {"cell_id": cell_id, "family": "ANCHOR_DIFF", "variant": variant,
            "target": target, "mean_ic": mean_ic, "t": t, "n_sessions": n,
            "extra": "paired per-session IC(r)-IC(disp) difference; "
                     "clustered t on the differences"}


def _extra_for(cell_id, fam, variant, target, fits, summ):
    """Family-specific ``extra``: M2 CV for E1-global / E2 / E3 (and E3-linear
    which IS the E1 W15/global fit); the no-intercept reading for E4/E5; a short
    label otherwise."""
    if fam == "E1" and "global" in variant:
        w = 15 if "W15" in variant else 30
        return "M2 LOWO-CV: " + _cv_extra(fits["e1"][w]["cv"], "b")
    if fam == "E1" and "persession" in variant:
        return "per-session beta = LOOKAHEAD diagnostic upper bound (spec §1)"
    if fam == "E2":
        lam = _lam_for_variant(variant)
        return "M2 LOWO-CV: " + _cv_extra(fits["e2"][lam]["cv"], "amplitude")
    if fam == "E3" and variant == "sqrt":
        return "M2 LOWO-CV: " + _cv_extra(fits["e3"]["cv"], "g")
    if fam == "E3" and variant == "linear":
        return ("linear arm == E1 W15/global (re-reported, no de-dup; spec §1). "
                "M2 LOWO-CV: " + _cv_extra(fits["e1"][15]["cv"], "b"))
    if fam in ("E4", "E5"):
        noint = _noint_counterpart(cell_id)
        if noint in summ.index:
            ni_ic = float(summ.loc[noint, "mean_ic"])
            ni_t = float(summ.loc[noint, "t"])
            return (f"canonical=with-intercept (spec §0 fair-mean residual); "
                    f"no-intercept reading mean_ic={ni_ic:.4g}, t={ni_t:.4g} "
                    "(FLAG-both diagnostic, not a grid cell)")
        return "canonical=with-intercept (spec §0 fair-mean residual)"
    if fam == "ANCHOR" and variant == "vwap_disp_30_1b":
        return "1b vwap-displacement object, re-reported for Phase-1b continuity"
    if fam == "ANCHOR":
        return "unconditional energy anchor"
    return ""


def _lam_for_variant(variant):
    """Recover the kernel lambda float from an E2 variant tag ('lam15' -> 15.0)."""
    tag = variant.replace("lam", "")
    for lam in E2_LAMBDAS:
        if _lam_tag(lam) == tag:
            return lam
    return float(tag)


# ==========================================================================
# 11. deterministic grid ordering
# ==========================================================================
_FAMILY_ORDER = {"E1": 0, "E2": 1, "E3": 2, "E4": 3, "E5": 4,
                 "ANCHOR": 5, "ANCHOR_DIFF": 6}
_TARGET_ORDER = {t: i for i, t in enumerate(TARGETS)}


def _sort_grid(grid_df):
    """Sort by (family, variant, target) deterministically so the grid /
    parquet row order is byte-stable run-to-run."""
    g = grid_df.copy()
    g["_fo"] = g["family"].map(lambda f: _FAMILY_ORDER.get(f, 99))
    g["_to"] = g["target"].map(lambda t: _TARGET_ORDER.get(t, 99))
    g = g.sort_values(["_fo", "variant", "_to"], kind="stable")
    g = g.drop(columns=["_fo", "_to"]).reset_index(drop=True)
    return g


# ==========================================================================
# 12. expected cell counts (pre-enumerated; asserted by the tests)
# ==========================================================================
def expected_cell_counts():
    """The pre-enumerated per-family GRID cell counts (spec §1). Diagnostics
    (no-intercept E4/E5) and the M2 CV are NOT grid cells and never appear
    here."""
    nT = len(TARGETS)
    return {
        "E1": len(E1_WINDOWS) * len(E1_BETA_MODES) * nT,   # 2×2×3 = 12
        "E2": len(E2_LAMBDAS) * nT,                        # 3×3   = 9
        "E3": len(E3_VARIANTS) * nT,                       # 2×3   = 6
        "E4": E4_N_TERCILES * nT,                          # 3×3   = 9
        "E5": len(E5_SIGN_SUBSETS) * nT,                   # 2×3   = 6
        "ANCHOR": 5 * nT,                                  # 5×3   = 15
        "ANCHOR_DIFF": nT,                                 # 1×3   = 3
    }


# ==========================================================================
# 13. parquet writer + thin CACHE-ONLY driver
# ==========================================================================
def write_grid(grid_df, analysis_dir=DEFAULT_ANALYSIS_DIR):
    """Write the tidy grid to ``bflow_phase1c_energy_grid.parquet``. Returns the
    path. Creates the directory."""
    os.makedirs(analysis_dir, exist_ok=True)
    path = os.path.join(analysis_dir, "bflow_phase1c_energy_grid.parquet")
    grid_df.to_parquet(path, index=False)
    return path


def run(cache_dir=DEFAULT_CACHE_DIR, insample_end=INSAMPLE_END,
        analysis_dir=DEFAULT_ANALYSIS_DIR, progress=True):
    """Full CACHE-ONLY pass: build pools -> fit -> score -> tidy grid -> parquet.
    Returns (grid_df, path)."""
    pools = build_session_pools(cache_dir, insample_end, progress=progress)
    grid_df, _fits = build_grid(pools, insample_end=insample_end)
    path = write_grid(grid_df, analysis_dir)
    if progress:
        print(f"[p1c-energy] {len(pools)} sessions; "
              f"{len(grid_df)} grid rows -> {path}", flush=True)
    return grid_df, path


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="energy_grid")
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                   help="minute-bar cache dir (CACHE-ONLY; never fetched)")
    p.add_argument("--insample-end", default=INSAMPLE_END,
                   help="last in-sample session (inclusive); the global causal "
                        "fits use sessions <= this (spec §1 PRIMARY)")
    p.add_argument("--analysis-dir", default=DEFAULT_ANALYSIS_DIR,
                   help="output dir for the energy-grid parquet")
    return p.parse_args(argv)


def main(argv=None):   # pragma: no cover
    args = _parse_args([] if argv is None else argv)
    grid_df, path = run(args.cache_dir, args.insample_end, args.analysis_dir)
    print(grid_df.to_string(index=False))
    print(f"[p1c-energy] wrote {path}", flush=True)
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main(sys.argv[1:]))
