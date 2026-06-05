"""SP-6 B-flow Phase 1c — flow+price JOINT conditional-IC menu (EXPLORATORY).

IN-SAMPLE EXPLORATORY — HYPOTHESIS GENERATION ONLY. Nothing this module
computes is a result. Every cell of the menu below was pre-enumerated (see
``MENU_DESCRIPTION`` / ``count_cells``); none was selected post-hoc, and no cell
is added after results are seen. With n~31-34 calm-tape in-sample sessions and
~2-3 effectively-independent tests across correlated features/horizons, any
promising cell is a *candidate for the Phase-1c pre-registration* and ~4-7 weeks
of OOS accrual away from being testable. More conditions/knobs => MORE researcher
degrees of freedom => LOWER robustness at this n, not higher.

This module adds NO policy/Δbps object whatsoever. It stays entirely on IC
(rank-correlation) objects, which are immune to the order-book direction-drift
confound that discredited the Phase-1b Test-B +22bps number.

Statistic (identical to Phase 1b, reused verbatim):
  * per-session pooled Spearman rank IC over all valid (ticker, minute) pairs
    (``predictability._spearman_ic``), one IC per session per cell;
  * across-session mean IC, sd IC, and clustered t = mean / (sd/sqrt(n_sessions))
    via ``predictability.summarize``. The SESSION is the cluster — never a
    pooled SE.

Universe (identical to Phase 1b Test-A):
  * the cached (ticker, session) pairs (``data/cache/min_bars``),
  * the Phase-1 60-valid-bar floor (``predictability.MIN_VALID_BARS``),
  * default scope = the 34 in-sample sessions (session <= 2026-06-02). Sessions
    after that are the labeled supplement and are EXCLUDED by default so the
    universe matches Phase 1b exactly.

THE FIXED MENU (every cell reported; none selected post-hoc):
  A. ANCHOR  — unconditional IC of {ofi_15, vwap_disp_30} vs the 3 targets.
               Must reproduce Phase 1b (sanity gate). 2x3 = 6 cells.
  B. RESIDUAL DISPLACEMENT — per-session pooled-OLS residual of vwap_disp_30 on
               ofi_15 (contemporaneous): disp_resid = vwap_disp_30 - (a+b*ofi_15).
               IC(disp_resid -> each target) AND IC(raw vwap_disp_30 -> each
               target) for comparison. Question on record: does price
               displacement carry predictive content BEYOND what contemporaneous
               flow explains? 2x3 = 6 cells.
  C. CONFIRM CONDITIONING — IC(ofi_15 -> each target) within the 4 fixed
               quadrants by (sign(ofi_15) x sign(vwap_disp_30)). 4x3 = 12 cells.
               Reports median per-session n per quadrant.
  D. INTERACTION — f_conf = ofi_15 * 1{sign(vwap_disp_30)==sign(ofi_15)}; IC vs
               each target, against plain ofi_15. 2x3 = 6 cells.
  E. BURST-ONLY — IC(ofi_15 -> targets) restricted to top-tercile |ofi_15| per
               session, with and without the C sign-agreement condition.
               2x3 = 6 cells.
  F. REGIME SLICE — A and C re-sliced by session regime (order_set.session_
               regime_map). Cell count = (n_A + n_C) x (regimes present).

The TARGET set is fixed once for the whole menu: {ret_fwd_15, ret_fwd_30,
ret_to_dump} (the A-named 3-set; "each target" in B-F means exactly these).

sign(0) convention: an exact-zero feature value is EXCLUDED from any quadrant /
sign-agreement subset (it has no sign). Stated once here, applied uniformly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.bflow import flow_features as ff
from src.research.bflow import oracle
from src.research.bflow import predictability as pr

# ---- fixed target set for the whole menu (the A-named 3-set) ----------------
TARGETS = ("ret_fwd_15", "ret_fwd_30", "ret_to_dump")

# Anchor features (A); the joint object is ofi_15 x vwap_disp_30 throughout.
FLOW = "ofi_15"
PRICE = "vwap_disp_30"

INSAMPLE_END = "2026-06-02"   # Phase-1b 34-session in-sample universe boundary
MIN_VALID_BARS = pr.MIN_VALID_BARS
DEFAULT_CACHE_DIR = "/root/openclaw/data/cache/min_bars"

# Number of quantile groups for the burst (E) top-tercile restriction.
_BURST_TERCILE = 3


# --------------------------------------------------------------------------
# per-(ticker, session) joint frame: features + targets, ROW-ALIGNED
# --------------------------------------------------------------------------
def _ticker_joint_frame(tdf, dump_price):
    """Return a single minute-indexed frame for ONE (ticker, session) carrying
    the flow feature, the price feature, and the 3 targets, all on the SAME row
    axis (minute 0..389). Row-alignment is load-bearing: every conditional mask
    in C/D/E is applied to feature and target jointly off this frame, so a
    residual / mask can never desync from its target.

    Pure: reuses ``flow_features.compute_features`` / ``compute_targets``
    verbatim (same windows, NaN rules, t-ranges as Phase 1b)."""
    feats = ff.compute_features(tdf)              # ofi_5, ofi_15, vwap_disp_30
    tgts = ff.compute_targets(tdf, dump_price)    # ret_to_dump, ret_fwd_*
    out = pd.DataFrame(index=feats.index)
    out[FLOW] = feats[FLOW]
    out[PRICE] = feats[PRICE]
    for t in TARGETS:
        out[t] = tgts[t]
    return out


def _session_joint_frames(session_bars, dump_prices):
    """List of per-ticker joint frames for the qualifying tickers in a session
    (those passing the 60-valid-bar floor), in sorted-ticker order. Mirrors the
    Phase-1b gate-1 filter exactly."""
    frames = []
    for ticker in sorted(session_bars):
        tdf = session_bars[ticker]
        if pr._valid_bar_count(tdf) < MIN_VALID_BARS:
            continue
        frames.append(_ticker_joint_frame(tdf, dump_prices.get(ticker)))
    return frames


# --------------------------------------------------------------------------
# residual displacement (B): per-session pooled OLS residual of PRICE on FLOW
# --------------------------------------------------------------------------
def _pooled_ols_residual(pooled_price, pooled_flow):
    """disp_resid = pooled_price - (a + b*pooled_flow), OLS fit on both-finite
    rows ONLY, residual placed back on the ORIGINAL row positions (NaN where
    either input is NaN). Pooled across (ticker, minute) within one session.

    Degenerate guards: <2 both-finite rows, or zero-variance flow -> residual is
    all-NaN for that session (cell contributes nothing -> drops from n_sessions,
    same convention as a zero-variance IC)."""
    price = pd.Series(np.asarray(pooled_price, dtype=float)).reset_index(drop=True)
    flow = pd.Series(np.asarray(pooled_flow, dtype=float)).reset_index(drop=True)
    fit_mask = price.notna() & flow.notna()
    out = pd.Series(np.nan, index=price.index)
    if int(fit_mask.sum()) < 2:
        return out
    x = flow[fit_mask].to_numpy()
    y = price[fit_mask].to_numpy()
    xc = x - x.mean()
    sxx = float((xc * xc).sum())
    if not (sxx > 0):           # zero-variance flow -> slope undefined
        return out
    b = float((xc * (y - y.mean())).sum() / sxx)
    a = float(y.mean() - b * x.mean())
    resid = price - (a + b * flow)   # NaN propagates where either input NaN
    return resid


# --------------------------------------------------------------------------
# per-session IC builders for each menu section
# --------------------------------------------------------------------------
def _ic_anchor(frames):
    """A: unconditional IC of FLOW and PRICE vs each target (this session)."""
    if not frames:
        return {}
    flow = pd.concat([f[FLOW] for f in frames], ignore_index=True)
    price = pd.concat([f[PRICE] for f in frames], ignore_index=True)
    tgt = {t: pd.concat([f[t] for f in frames], ignore_index=True) for t in TARGETS}
    out = {}
    for t in TARGETS:
        out[f"A:{FLOW}->{t}"] = pr._spearman_ic(flow, tgt[t])
        out[f"A:{PRICE}->{t}"] = pr._spearman_ic(price, tgt[t])
    return out


def _ic_residual(frames):
    """B: IC(disp_resid -> target) AND IC(raw PRICE -> target). The raw-PRICE
    row is byte-identical to A's PRICE row (same object) — a built-in check."""
    if not frames:
        return {}
    flow = pd.concat([f[FLOW] for f in frames], ignore_index=True)
    price = pd.concat([f[PRICE] for f in frames], ignore_index=True)
    tgt = {t: pd.concat([f[t] for f in frames], ignore_index=True) for t in TARGETS}
    resid = _pooled_ols_residual(price, flow)
    out = {}
    for t in TARGETS:
        out[f"B:disp_resid->{t}"] = pr._spearman_ic(resid, tgt[t])
        out[f"B:raw_{PRICE}->{t}"] = pr._spearman_ic(price, tgt[t])
    return out


_QUADRANTS = (
    ("flowDn_priceDn", -1, -1),   # confirmed sell pressure (theory: fade works)
    ("flowDn_priceUp", -1, +1),   # absorbed selling (theory: do NOT fade)
    ("flowUp_priceUp", +1, +1),
    ("flowUp_priceDn", +1, -1),
)


def _ic_quadrant(frames):
    """C: IC(FLOW -> target) within each (sign(FLOW) x sign(PRICE)) quadrant.
    Exact-zero feature values are excluded (no sign). Returns the IC dict AND a
    per-quadrant observation count {quad: n_obs_this_session} for the median-n
    report."""
    if not frames:
        return {}, {}
    flow = pd.concat([f[FLOW] for f in frames], ignore_index=True)
    price = pd.concat([f[PRICE] for f in frames], ignore_index=True)
    tgt = {t: pd.concat([f[t] for f in frames], ignore_index=True) for t in TARGETS}
    sgn_f = np.sign(flow)
    sgn_p = np.sign(price)
    out = {}
    counts = {}
    for qname, sf, sp in _QUADRANTS:
        mask = (sgn_f == sf) & (sgn_p == sp)   # exact zeros excluded (sign 0)
        counts[qname] = int(mask.sum())
        fsub = flow.where(mask)
        for t in TARGETS:
            out[f"C:{qname}|{FLOW}->{t}"] = pr._spearman_ic(fsub, tgt[t].where(mask))
    return out, counts


def _ic_interaction(frames):
    """D: f_conf = FLOW * 1{sign(PRICE)==sign(FLOW)} (0 where signs disagree, and
    0 where either feature is exactly 0 — both are 'not confirmed'). IC vs each
    target, plus plain FLOW for comparison. The zero block compresses rank but is
    the defined interaction object."""
    if not frames:
        return {}
    flow = pd.concat([f[FLOW] for f in frames], ignore_index=True)
    price = pd.concat([f[PRICE] for f in frames], ignore_index=True)
    tgt = {t: pd.concat([f[t] for f in frames], ignore_index=True) for t in TARGETS}
    agree = (np.sign(flow) == np.sign(price)) & (np.sign(flow) != 0)
    # f_conf = FLOW where signs agree, else 0; NaN preserved where FLOW is NaN.
    f_conf = flow.where(agree, other=0.0).where(flow.notna())
    out = {}
    for t in TARGETS:
        out[f"D:f_conf->{t}"] = pr._spearman_ic(f_conf, tgt[t])
        out[f"D:plain_{FLOW}->{t}"] = pr._spearman_ic(flow, tgt[t])
    return out


def _ic_burst(frames):
    """E: IC(FLOW -> target) restricted to the top-tercile |FLOW| observations
    (per session pool), with and without the C sign-agreement condition.

    The tercile cut is computed on the pooled |FLOW| within the session (the
    'per session' restriction in the spec), so 'burst' is session-relative."""
    if not frames:
        return {}
    flow = pd.concat([f[FLOW] for f in frames], ignore_index=True)
    price = pd.concat([f[PRICE] for f in frames], ignore_index=True)
    tgt = {t: pd.concat([f[t] for f in frames], ignore_index=True) for t in TARGETS}
    absf = flow.abs()
    finite = absf.dropna()
    out = {}
    if len(finite) < _BURST_TERCILE:
        for t in TARGETS:
            out[f"E:burst->{t}"] = float("nan")
            out[f"E:burst_confirmed->{t}"] = float("nan")
        return out
    cut = finite.quantile(1.0 - 1.0 / _BURST_TERCILE)   # top-tercile threshold
    burst = absf >= cut
    agree = (np.sign(flow) == np.sign(price)) & (np.sign(flow) != 0)
    burst_conf = burst & agree
    for t in TARGETS:
        out[f"E:burst->{t}"] = pr._spearman_ic(flow.where(burst), tgt[t].where(burst))
        out[f"E:burst_confirmed->{t}"] = pr._spearman_ic(
            flow.where(burst_conf), tgt[t].where(burst_conf))
    return out


# --------------------------------------------------------------------------
# whole-session driver
# --------------------------------------------------------------------------
def session_cells(session_bars, dump_prices):
    """All non-regime menu cells (A,B,C,D,E) for ONE session, plus the C
    per-quadrant observation counts. Returns (ic_dict, quad_counts)."""
    frames = _session_joint_frames(session_bars, dump_prices)
    ics = {}
    ics.update(_ic_anchor(frames))
    ics.update(_ic_residual(frames))
    cq, counts = _ic_quadrant(frames)
    ics.update(cq)
    ics.update(_ic_interaction(frames))
    ics.update(_ic_burst(frames))
    return ics, counts


# --------------------------------------------------------------------------
# cache loading (CACHE-ONLY; mirrors run_phase1b.load_session_frame)
# --------------------------------------------------------------------------
def _load_session_frame(cache_dir, session):
    import os
    path = os.path.join(cache_dir, f"min_bars_{session}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if "minute" in df.columns:
        df = df[pd.to_numeric(df["minute"], errors="coerce") >= 0]
    return df


def _ticker_frames(session_df):
    out = {}
    for tk in sorted(t for t in session_df["ticker"].dropna().unique()):
        out[str(tk)] = session_df[session_df["ticker"] == tk].copy()
    return out


def enumerate_sessions(cache_dir, insample_end=INSAMPLE_END):
    """Sorted cache sessions with session <= insample_end (the Phase-1b 34-session
    in-sample universe by default)."""
    import os
    import re
    rx = re.compile(r"^min_bars_(\d{4}-\d{2}-\d{2})\.parquet$")
    if not os.path.isdir(cache_dir):
        return []
    out = []
    for name in os.listdir(cache_dir):
        m = rx.match(name)
        if m and m.group(1) <= insample_end:
            out.append(m.group(1))
    return sorted(out)


def run_menu(cache_dir=DEFAULT_CACHE_DIR, insample_end=INSAMPLE_END, progress=True):
    """Run the full A-E menu over every in-sample cache session. Returns
    ``(per_session_ics, quad_counts_per_session, regime_per_session, sessions)``:
      * per_session_ics: list of (session, {cell: ic})
      * quad_counts_per_session: list of (session, {quad: n_obs})
      * regime_per_session: dict session -> regime (None if unavailable)
    """
    sessions = enumerate_sessions(cache_dir, insample_end)
    per_session_ics = []
    quad_counts = []
    for session in sessions:
        sdf = _load_session_frame(cache_dir, session)
        if sdf is None or not len(sdf):
            if progress:
                print(f"[p1c] {session}: MISSING -> skipped", flush=True)
            continue
        frames = _ticker_frames(sdf)
        dump = {tk: oracle.dump_benchmark(tdf.to_dict("records"))
                for tk, tdf in frames.items()}
        ics, counts = session_cells(frames, dump)
        per_session_ics.append((session, ics))
        quad_counts.append((session, counts))
        if progress:
            nq = sum(1 for tk, tdf in frames.items()
                     if pr._valid_bar_count(tdf) >= MIN_VALID_BARS)
            print(f"[p1c] {session}: {len(frames)} tickers, {nq} pass floor",
                  flush=True)
    regime_map = _safe_regime_map()
    regime_per_session = {s: regime_map.get(s) for s, _ in per_session_ics}
    return per_session_ics, quad_counts, regime_per_session, sessions


def _safe_regime_map():
    """order_set.session_regime_map (DB-backed); {} on any failure so F degrades
    to 'regime unavailable' rather than crashing the menu."""
    try:
        from src.research.bflow import order_set
        return order_set.session_regime_map()
    except Exception as e:   # noqa: BLE001 - degrade, never crash the menu
        print(f"[p1c] regime map unavailable: {type(e).__name__}: {e}", flush=True)
        return {}


# --------------------------------------------------------------------------
# summarisation: reuse predictability.summarize via a cell-keyed grid
# --------------------------------------------------------------------------
def _summarize_cells(per_session_ics):
    """mean_ic / sd_ic / n_sessions / t per cell, using the SAME clustered-t
    convention as Phase 1b (predictability.summarize: sample sd ddof=1, n<2 ->
    NaN). Builds a sessions x cells grid then summarizes."""
    cells = []
    seen = set()
    for _, ics in per_session_ics:
        for c in ics:
            if c not in seen:
                seen.add(c)
                cells.append(c)
    cells = sorted(cells)
    data = {c: [] for c in cells}
    idx = []
    for s, ics in per_session_ics:
        idx.append(s)
        for c in cells:
            v = ics.get(c, float("nan"))
            data[c].append(np.nan if v is None else v)
    grid = pd.DataFrame(data, index=idx, columns=cells)
    grid.index.name = "session"
    # Reuse the Phase-1b summarizer cell-by-cell (its CELLS list is the Phase-1b
    # 15; here we summarize an arbitrary cell set with the identical math).
    rows = {}
    for c in cells:
        vals = grid[c].dropna()
        n = int(len(vals))
        mean_ic = float(vals.mean()) if n > 0 else float("nan")
        sd_ic = float(vals.std(ddof=1)) if n > 1 else float("nan")
        t = mean_ic / (sd_ic / np.sqrt(n)) if (n > 1 and sd_ic > 0) else float("nan")
        rows[c] = {"mean_ic": mean_ic, "sd_ic": sd_ic, "n_sessions": n, "t": t}
    summ = pd.DataFrame.from_dict(rows, orient="index",
                                  columns=["mean_ic", "sd_ic", "n_sessions", "t"])
    summ.index.name = "cell"
    return grid, summ


# --------------------------------------------------------------------------
# menu cell-count enumeration (pre-registered before any run)
# --------------------------------------------------------------------------
def count_cells(regimes_present):
    """Exact menu cell count, enumerated BEFORE running. regimes_present is the
    list of distinct regimes that appear among the in-sample sessions (F
    re-slices A and C by each)."""
    n_A = 2 * len(TARGETS)          # {FLOW, PRICE} x targets
    n_B = 2 * len(TARGETS)          # {disp_resid, raw PRICE} x targets
    n_C = 4 * len(TARGETS)          # 4 quadrants x targets
    n_D = 2 * len(TARGETS)          # {f_conf, plain FLOW} x targets
    n_E = 2 * len(TARGETS)          # {burst, burst_confirmed} x targets
    base = n_A + n_B + n_C + n_D + n_E
    n_F = (n_A + n_C) * len(regimes_present)   # A and C re-sliced per regime
    return {"A": n_A, "B": n_B, "C": n_C, "D": n_D, "E": n_E,
            "base": base, "F": n_F, "total": base + n_F}


MENU_DESCRIPTION = (
    "A anchor (2x3) + B residual (2x3) + C quadrant (4x3) + D interaction (2x3) "
    "+ E burst (2x3) = 36 base cells; F re-slices A and C (18 cells) by each "
    "regime present."
)
