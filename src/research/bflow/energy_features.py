"""SP-6 B-flow Phase 1c — ENERGY-model construction layer (PURE, no I/O).

T1 of the Phase-1c ENERGY deep-dive. Binding spec:
``analysis/bflow_phase1c_energy_grid_preenumeration.md`` (committed pre-results;
the grid may not be expanded or shrunk). This module is the CONSTRUCTION layer
ONLY — it builds the window features the harness lacks and the E1/E2/E3 energy
residuals with their fitted scalars, plus the M2 leave-one-week-out CV of every
fitted scalar. It computes NO information coefficients: the IC grid (M1), the
|r|-tercile split (E4), the sign(r) split (E5) and the symmetric two-leg
execution counterfactual (M3) are DOWNSTREAM tasks (the IC primitive already
lives in ``predictability._spearman_ic`` for them to reuse). If anything in this
file simulated a fill or computed an IC, it would have left T1.

Everything is pure (frame-in / frame-out), mirroring the Phase-1b harness
idioms: features are built on the ``flow_features._reindex_valid_frame`` seam,
all OLS fits reuse the exact-recoverable least-squares convention of
``exploratory_phase1c._pooled_ols_residual`` (with an intercept), and the
clustered-statistic conventions live downstream unchanged.

SIGN CONVENTION (fixed, spec §1): every downstream cell reports IC(r → target);
the energy/reversion hypothesis ⟺ NEGATIVE IC. T1 only constructs r, so the sign
convention is carried, not exercised, here.


WINDOW FEATURES THE GRID NEEDS (spec §1 item 1)
-----------------------------------------------
ofi_30
    Reuses ``flow_features._ofi(work, dc, 30)`` VERBATIM — same trailing
    sign(Δc)·v / Σv ratio, ``min_periods=30``, same NaN / first-bar-predecessor
    rule as ofi_5 / ofi_15. No new definition.

disp_W  (W ∈ {15, 30})  — the spec's "return over trailing W min"
    DOCUMENTED CHOICE (a): disp_W_t = c_t / c_{t−W} − 1, a CLOSE-to-close return
    on column ``c`` off the ``_reindex_valid_frame`` work-frame. This is a
    DEPARTURE from the harness ``vwap_disp_30`` object, which is a
    VWAP-DISPLACEMENT (a level vs a trailing volume-weighted price), NOT a
    return. The spec text plainly says "return over trailing W min", and the
    grounding-agent recommendation matches: OFI_W's per-minute sign(Δc)·v deltas
    telescope over exactly the span c_t − c_{t−W}, so a close-to-close return
    over the matched W-span is the spec's "realized move vs flow" pairing
    (E1 r = disp_W − β·OFI_W). disp_W REPLACES vwap_disp_30 in EVERY energy cell
    including the anchor (the energy anchor is disp_15 + OFI_15, NOT the
    exploratory vwap_disp_30 anchor).

    DOCUMENTED CHOICE (b): endpoint-validity — disp_W is NaN unless BOTH c_t and
    c_{t−W} are valid bars (we read them off the reindexed work-frame where
    invalid/missing bars are already NULLed, so a NaN at either endpoint
    propagates through the ratio). This is the literal "return over trailing W
    min" reading; the residual support intersects OFI_W's full-window NaN rule
    either way (OFI_W is itself NaN on any incomplete/invalid window), so the
    pairing used in E1/E2/E3 is unaffected by the endpoint-vs-full-window choice.
    Earliest finite disp_W is minute W (c_{t−W} = c_0 is the earliest valid
    reference).


E1 STATIC LINEAR RESIDUAL (spec §1 item 2):  r = disp_W − β·OFI_W
----------------------------------------------------------------
Two β modes:
  * GLOBAL CAUSAL (PRIMARY) — ``fit_global_linear`` pools ALL both-finite
    (disp_W, OFI_W) pairs across the sessions with date ≤ insample_end
    (default "2026-06-02"), fits ONE (a, b) by ordinary least squares, and that
    frozen (a, b) is applied to EVERY session via ``apply_linear_residual``. The
    cutoff is filtered INTERNALLY by the fitter — that internal filter is what
    makes the causality invariant hold: appending a session dated after the
    cutoff cannot change β. This fitter is NEW (it does not exist in the harness;
    ``_pooled_ols_residual`` is per-session and is the lookahead diagnostic, not
    the causal primary).
  * PER-SESSION (LOOKAHEAD diagnostic upper bound) — ``e1_residual_per_session``
    reuses ``exploratory_phase1c._pooled_ols_residual`` VERBATIM (whole-session
    fit embeds future minutes in r_t). Exposed under ``E1_PER_SESSION_MODE`` so
    the API labels it lookahead.

INTERCEPT (load-bearing flag, NOT resolved here — spec writes r = disp_W − β·OFI_W
with NO intercept; the harness pooled-OLS fits a + b·flow WITH an intercept):
A constant shift is IMMATERIAL to E1/E2/E3 ICs (a monotone transform leaves the
Spearman rank-IC identical) but it FLIPS membership in E5's sign(r) split and
shifts E4's |r| terciles. Both readings are defensible. T1 does not choose: every
global fitter RETURNS BOTH coefficients (a and b), and ``apply_linear_residual``
forms the WITH-intercept residual by default; a downstream consumer that wants
the no-intercept reading subtracts only b·OFI_W (i.e. adds ``fit['a']`` back).
Returning both coeffs lets E4/E5 run under either reading with NO refit. We use
ONE OLS convention (with-intercept) uniformly across E1/E2/E3 so the intercept
story is identical everywhere.


E2 PROPAGATOR / KERNEL RESIDUAL (spec §1 item 3)
------------------------------------------------
M_t = Σ_{τ≥0} G(τ)·flow_{t−τ}, G(τ) = exp(−τ/λ), λ ∈ {5, 15, 45} min; the single
global amplitude scalar ``a`` is fit by pooled OLS of the realized cumulative
session return on M_t over sessions ≤ insample_end; r = realized_cum_t − a·M_t.

PINNED (the spec leaves these undefined; pinned here, documented, NOT scanned):
  * realized_cum_t = c_t / c_{first_valid_minute} − 1 — an expanding CAUSAL
    window from the first valid bar (NaN before/at the reference is impossible
    once the first valid bar exists; it is 0 AT the first valid minute and NaN
    only where c_t itself is invalid/missing). This is the one fully-pinned leg
    in the grounding map.
  * flow_{t−τ} ("ofi-flow", UNDEFINED in the spec) = the per-minute signed-flow
    increment sign(Δc_t)·v_t — i.e. the OFI NUMERATOR, the natural pin (the OFI
    ratio's signed-volume term, the same Δc / sign / volume objects OFI_W sums).
    The two alternatives flagged by the grounding map — (ii) the OFI ratio
    window value, (iii) signed DOLLAR volume sign(Δc_t)·v_t·vw_t — are NOT
    scanned (standing constraint: pin one, document, move on).
  * convolution gap handling: an invalid/missing minute has flow_{t−τ} = NaN
    (Δc and v are NULLed by ``_reindex_valid_frame``). We treat a NaN flow
    increment as a ZERO contribution inside the causal τ-sum (``fillna(0)``
    before convolving) so a single gap does not annihilate the whole prefix sum;
    the realized_cum leg already carries the gap (NaN c_t) so any M_t paired with
    a NaN realized_cum drops from the fit anyway. Documented; not scanned.

The convolution is a strictly-causal exponential IIR: M_t = flow_t + e^{−1/λ}·M_{t−1}
(only past-and-present minutes contribute), implemented as an explicit causal
recursion so no future minute is ever read.


E3 CONCAVE IMPACT RESIDUAL (spec §1 item 4)
-------------------------------------------
E[disp] ∝ g·sign(OFI_15)·√|OFI_15| (square-root law) vs linear, W = 15, global
fit on ≤ insample_end; r = disp_15 − (a + g·term). The linear comparator IS the
E1 W=15/global object — no new object is built for it (spec §1: report E3-concave
and E1-W15/global BOTH, no de-dup; the de-dup guard is a downstream reporting
concern, not a construction one).


M2 LEAVE-ONE-WEEK-OUT CV (spec §1 item 5 / §2 M2)
-------------------------------------------------
For EVERY fitted scalar (each β per W, the E2 amplitude per λ, the E3 g): refit
on the OTHER ISO-weeks of the ≤ insample_end sample, score the held-out week,
cycle. Returns the in-sample fit and the per-held-out-week CV fits SIDE BY SIDE
(no IC evaluation enters T1 — M2 here returns the SCALARS, the in-sample-vs-CV
gap being the overfit diagnostic).

ISO-week key: ``iso_week_key(session)`` = ``(iso_year, iso_week)`` from
``datetime.date.fromisoformat(session).isocalendar()`` — deterministic, groups
calendar dates in the same ISO week (Mon–Sun) together. This is the only
date-arithmetic in the module and it touches NO clock (it parses the session
label string), so it does not violate the predicate no-clock contract.
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from src.research.bflow import flow_features as ff
from src.research.bflow import exploratory_phase1c as p1c

# In-sample cutoff — reuse the exploratory / Phase-1b boundary VERBATIM so the
# energy fit sample is the identical 34-session in-sample universe.
INSAMPLE_END = p1c.INSAMPLE_END  # "2026-06-02"

# disp_W spans the grid uses.
DISP_WINDOWS = (15, 30)
# OFI window paired with disp in E1 (one β per (W, mode)).
OFI_WINDOW_FOR_DISP = {15: 15, 30: 30}
# E2 kernel time-constants (minutes).
KERNEL_LAMBDAS = (5.0, 15.0, 45.0)
# E3 concave law uses W = 15.
CONCAVE_WINDOW = 15

# Mode labels (the per-session mode is FLAGGED lookahead in its name).
E1_GLOBAL_MODE = "global_causal"
E1_PER_SESSION_MODE = "per_session_lookahead"

_MINUTES = range(0, 390)


# ==========================================================================
# 1. window features — ofi_30 (reuse) + disp_W (new, close-return)
# ==========================================================================
def _disp_W(work, k):
    """disp_W_t = c_t / c_{t−k} − 1 off the reindexed work-frame's close column.

    Endpoint-validity: ``work['c']`` is already NaN on invalid/missing bars
    (``_reindex_valid_frame`` NULLs them), so the ratio is NaN whenever EITHER
    c_t or c_{t−k} is invalid/missing. Earliest finite value is minute k."""
    c = work["c"]
    c_prev = c.shift(k)
    return c / c_prev - 1.0


def compute_energy_features(df):
    """Energy window features for ONE (ticker, session) frame. Returns a
    DataFrame indexed by minute 0..389 with columns
    ``ofi_5, ofi_15, ofi_30, disp_15, disp_30`` (ofi_5/ofi_15 carried so a
    consumer has the full ladder; the energy grid needs ofi_30 + disp_W).

    Pure; strictly trailing — no value at minute t depends on any bar after t.
    Built on the same ``_reindex_valid_frame`` seam as ``compute_features`` so
    the validity / dedup / NaN rules are byte-identical to the harness."""
    work, _valid = ff._reindex_valid_frame(df)
    dc = ff._delta_c(work)

    out = pd.DataFrame(index=pd.Index(_MINUTES, name="minute"))
    # ofi ladder — reuse ff._ofi verbatim (no new definition).
    out["ofi_5"] = ff._ofi(work, dc, 5)
    out["ofi_15"] = ff._ofi(work, dc, 15)
    out["ofi_30"] = ff._ofi(work, dc, 30)
    # disp_W close-return (documented choice a/b above).
    for w in DISP_WINDOWS:
        out[f"disp_{w}"] = _disp_W(work, w)
    return out


# ==========================================================================
# 2. shared least-squares primitive (with intercept) — exact-recoverable
# ==========================================================================
def _ols_fit(y, x):
    """Ordinary least squares y = a + b·x on both-finite rows ONLY. Returns
    (a, b). Degenerate (fewer than 2 both-finite rows, or zero-variance x) ->
    (NaN, NaN). Mirrors ``exploratory_phase1c._pooled_ols_residual``'s fit math
    exactly (same xc·yc / sxx slope, same intercept), so a single-session global
    fit and the per-session residual coincide."""
    y = pd.Series(np.asarray(y, dtype=float)).reset_index(drop=True)
    x = pd.Series(np.asarray(x, dtype=float)).reset_index(drop=True)
    mask = y.notna() & x.notna()
    if int(mask.sum()) < 2:
        return float("nan"), float("nan")
    xv = x[mask].to_numpy()
    yv = y[mask].to_numpy()
    xc = xv - xv.mean()
    sxx = float((xc * xc).sum())
    if not (sxx > 0):
        return float("nan"), float("nan")
    b = float((xc * (yv - yv.mean())).sum() / sxx)
    a = float(yv.mean() - b * xv.mean())
    return a, b


def _insample_sessions(bundles, insample_end):
    """The bundle keys (session date strings) with date ≤ insample_end, sorted.
    THIS internal filter is what makes the causality invariant hold: a
    post-cutoff session is never part of any global fit."""
    return sorted(s for s in bundles if s <= insample_end)


# ==========================================================================
# 3. E1 — global causal OLS (PRIMARY) + per-session (lookahead diagnostic)
# ==========================================================================
def fit_global_linear(bundles, insample_end=INSAMPLE_END):
    """GLOBAL CAUSAL OLS for E1: pool every both-finite (disp_W, OFI_W) pair
    across the in-sample sessions and fit ONE (a, b).

    ``bundles``: dict session_date -> {'flow': Series(OFI_W), 'price': Series
    (disp_W)} (the per-session pre-extracted pairs; both Series share a row
    index). Sessions dated > insample_end are filtered out INTERNALLY.

    Returns {'a', 'b', 'mode', 'n_obs', 'n_sessions'}; (a, b) are (NaN, NaN) on a
    degenerate pool (the same guard as the per-session residual). BOTH a and b
    are returned so downstream E4/E5 can form the with- or without-intercept r
    with no refit (the intercept flag, see module docstring)."""
    sessions = _insample_sessions(bundles, insample_end)
    prices, flows = [], []
    for s in sessions:
        b = bundles[s]
        prices.append(pd.Series(np.asarray(b["price"], dtype=float)))
        flows.append(pd.Series(np.asarray(b["flow"], dtype=float)))
    if prices:
        pooled_price = pd.concat(prices, ignore_index=True)
        pooled_flow = pd.concat(flows, ignore_index=True)
    else:
        pooled_price = pd.Series(dtype=float)
        pooled_flow = pd.Series(dtype=float)
    a, b = _ols_fit(pooled_price, pooled_flow)
    n_obs = int((pooled_price.notna() & pooled_flow.notna()).sum())
    return {"a": a, "b": b, "mode": E1_GLOBAL_MODE,
            "n_obs": n_obs, "n_sessions": len(sessions)}


def apply_linear_residual(price, flow, fit):
    """r = disp_W − (a + b·OFI_W) using the FROZEN global (a, b) in ``fit``. NaN
    propagates where either input is NaN. Default = with-intercept residual; a
    no-intercept consumer adds ``fit['a']`` back (r_no_intercept = r + a)."""
    price = pd.Series(np.asarray(price, dtype=float))
    flow = pd.Series(np.asarray(flow, dtype=float)).reset_index(drop=True)
    flow.index = price.index
    a = fit["a"]
    b = fit["b"]
    if not np.isfinite(a) or not np.isfinite(b):
        return pd.Series(np.nan, index=price.index)
    return price - (a + b * flow)


def e1_residual_per_session(price, flow):
    """PER-SESSION (LOOKAHEAD diagnostic) E1 residual. Reuses
    ``exploratory_phase1c._pooled_ols_residual`` VERBATIM — the whole-session
    fit embeds future minutes in r_t, so this is an optimistic upper bound, NOT
    the causal primary. Labeled via ``E1_PER_SESSION_MODE``."""
    return p1c._pooled_ols_residual(price, flow)


# ==========================================================================
# 4. E2 — propagator convolution + realized-cum leg + global amplitude
# ==========================================================================
def propagator_convolve(flow, lam):
    """Strictly-causal exponential propagator
    M_t = Σ_{τ≥0} exp(−τ/λ)·flow_{t−τ}, implemented as the IIR recursion
    M_t = flow_t + e^{−1/λ}·M_{t−1} (only past-and-present minutes contribute).

    NaN flow increments (invalid/missing minutes) are treated as ZERO
    contributions inside the sum (``fillna(0)``) so a single gap does not
    annihilate the prefix sum; the realized_cum leg carries the gap, so a paired
    NaN realized_cum still drops that row from the amplitude fit. Returns a
    Series on the input index."""
    f = pd.Series(np.asarray(flow, dtype=float))
    idx = f.index
    fv = np.nan_to_num(f.to_numpy(), nan=0.0)
    decay = math.exp(-1.0 / lam)
    out = np.empty_like(fv)
    acc = 0.0
    for i in range(len(fv)):
        acc = fv[i] + decay * acc
        out[i] = acc
    return pd.Series(out, index=idx)


def ofi_flow_increment(df):
    """The pinned per-minute "ofi-flow" increment = sign(Δc_t)·v_t (the OFI
    numerator), built on the ``_reindex_valid_frame`` seam so Δc / v are NaN on
    invalid/missing bars exactly as in ``flow_features``. Minute 0 uses
    Δc = c0 − o0 (``ff._delta_c``). Returns a Series on minute 0..389."""
    work, _valid = ff._reindex_valid_frame(df)
    dc = ff._delta_c(work)
    return np.sign(dc) * work["v"]


def realized_cum_return(df):
    """realized_cum_t = c_t / c_{first_valid_minute} − 1, an expanding CAUSAL
    window from the first valid bar. NaN where c_t is invalid/missing (and all
    NaN if there is no valid bar at all). Built on the reindexed work-frame."""
    work, valid = ff._reindex_valid_frame(df)
    c = work["c"]
    valid_minutes = c.index[valid.to_numpy()]
    if len(valid_minutes) == 0:
        return pd.Series(np.nan, index=c.index)
    c0 = float(c.loc[valid_minutes[0]])
    if not (c0 > 0):
        return pd.Series(np.nan, index=c.index)
    return c / c0 - 1.0


def fit_global_propagator(bundles, lam, insample_end=INSAMPLE_END):
    """GLOBAL amplitude fit for E2 at kernel time-constant ``lam``: pool every
    both-finite (realized_cum, M_t) pair across the in-sample sessions and fit a
    single amplitude by pooled OLS of realized_cum on M (with intercept, for the
    uniform-intercept convention).

    ``bundles``: dict session -> {'flow': Series(ofi-flow increment),
    'realized_cum': Series}. M_t is convolved here from ``flow`` so the kernel is
    applied identically to every session. Sessions > insample_end filtered out
    INTERNALLY (causality invariant). Returns {'amplitude', 'a', 'lam', 'n_obs',
    'n_sessions'} (amplitude = the slope; 'a' = the intercept)."""
    sessions = _insample_sessions(bundles, insample_end)
    ys, Ms = [], []
    for s in sessions:
        b = bundles[s]
        M = propagator_convolve(b["flow"], lam)
        ys.append(pd.Series(np.asarray(b["realized_cum"], dtype=float)).reset_index(drop=True))
        Ms.append(pd.Series(np.asarray(M, dtype=float)).reset_index(drop=True))
    if ys:
        pooled_y = pd.concat(ys, ignore_index=True)
        pooled_M = pd.concat(Ms, ignore_index=True)
    else:
        pooled_y = pd.Series(dtype=float)
        pooled_M = pd.Series(dtype=float)
    a, amp = _ols_fit(pooled_y, pooled_M)
    n_obs = int((pooled_y.notna() & pooled_M.notna()).sum())
    return {"amplitude": amp, "a": a, "lam": lam,
            "n_obs": n_obs, "n_sessions": len(sessions)}


def apply_propagator_residual(realized_cum, flow, lam, fit):
    """r = realized_cum_t − a·M_t using the FROZEN amplitude in ``fit``. The
    intercept is carried in fit['a'] (uniform-intercept convention); the
    default residual is realized_cum − (a + amplitude·M)."""
    M = propagator_convolve(flow, lam)
    rc = pd.Series(np.asarray(realized_cum, dtype=float))
    M = pd.Series(np.asarray(M, dtype=float))
    M.index = rc.index
    amp = fit["amplitude"]
    a = fit.get("a", 0.0)
    if not np.isfinite(amp) or not np.isfinite(a):
        return pd.Series(np.nan, index=rc.index)
    return rc - (a + amp * M)


# ==========================================================================
# 5. E3 — concave (square-root) impact law, global fit
# ==========================================================================
def concave_term(ofi15):
    """sign(OFI_15)·√|OFI_15| — the concave (square-root law) regressor. NaN
    propagates where OFI_15 is NaN; exact-zero OFI -> 0."""
    o = pd.Series(np.asarray(ofi15, dtype=float))
    return np.sign(o) * np.sqrt(o.abs())


def fit_global_concave(bundles, insample_end=INSAMPLE_END):
    """GLOBAL g fit for E3: pool every both-finite (disp_15, concave_term) pair
    across the in-sample sessions and fit a + g·term by pooled OLS.

    ``bundles``: dict session -> {'ofi15': Series, 'disp15': Series}. Sessions >
    insample_end filtered out INTERNALLY. Returns {'g', 'a', 'n_obs',
    'n_sessions'} (g = slope on the sqrt-term; 'a' = intercept)."""
    sessions = _insample_sessions(bundles, insample_end)
    disps, terms = [], []
    for s in sessions:
        b = bundles[s]
        term = concave_term(b["ofi15"])
        disps.append(pd.Series(np.asarray(b["disp15"], dtype=float)).reset_index(drop=True))
        terms.append(pd.Series(np.asarray(term, dtype=float)).reset_index(drop=True))
    if disps:
        pooled_disp = pd.concat(disps, ignore_index=True)
        pooled_term = pd.concat(terms, ignore_index=True)
    else:
        pooled_disp = pd.Series(dtype=float)
        pooled_term = pd.Series(dtype=float)
    a, g = _ols_fit(pooled_disp, pooled_term)
    n_obs = int((pooled_disp.notna() & pooled_term.notna()).sum())
    return {"g": g, "a": a, "n_obs": n_obs, "n_sessions": len(sessions)}


def apply_concave_residual(disp15, ofi15, fit):
    """r = disp_15 − (a + g·sign(OFI_15)·√|OFI_15|) using the FROZEN (a, g)."""
    term = concave_term(ofi15)
    disp = pd.Series(np.asarray(disp15, dtype=float))
    term = pd.Series(np.asarray(term, dtype=float))
    term.index = disp.index
    g = fit["g"]
    a = fit.get("a", 0.0)
    if not np.isfinite(g) or not np.isfinite(a):
        return pd.Series(np.nan, index=disp.index)
    return disp - (a + g * term)


# ==========================================================================
# 6. M2 — leave-one-week-out CV of every fitted scalar
# ==========================================================================
def iso_week_key(session):
    """(iso_year, iso_week) for a 'YYYY-MM-DD' session label. Deterministic;
    groups dates in the same ISO week (Mon–Sun) together. Parses the label
    string only — touches no clock."""
    d = date.fromisoformat(session)
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _weeks_of(bundles, insample_end):
    """Map ISO-week-key -> sorted list of in-sample session dates in that week."""
    weeks = {}
    for s in _insample_sessions(bundles, insample_end):
        weeks.setdefault(iso_week_key(s), []).append(s)
    for wk in weeks:
        weeks[wk].sort()
    return weeks


def _leave_one_week_out(bundles, insample_end, fit_fn):
    """Generic LOWO-CV harness: returns {'insample': fit_on_all,
    'cv_by_week': {week_key: fit_on_OTHER_weeks}}. ``fit_fn(sub_bundles)`` fits
    the scalar(s) on a session subset. For each in-sample ISO week, we refit on
    the sessions of the OTHER weeks and record that fit (the held-out week's
    out-of-sample parameter). The in-sample-vs-CV gap is the overfit
    diagnostic."""
    insample = fit_fn(bundles)
    weeks = _weeks_of(bundles, insample_end)
    cv = {}
    if len(weeks) >= 2:
        for held in weeks:
            others = {s: bundles[s] for wk, sess in weeks.items()
                      if wk != held for s in sess}
            cv[held] = fit_fn(others)
    return {"insample": insample, "cv_by_week": cv}


def cv_global_linear(bundles, insample_end=INSAMPLE_END):
    """M2 LOWO-CV of the E1 global β (one (a, b) per fold). in-sample fit beside
    the per-held-out-week refits."""
    return _leave_one_week_out(
        bundles, insample_end,
        lambda bd: fit_global_linear(bd, insample_end=insample_end))


def cv_global_propagator(bundles, lam, insample_end=INSAMPLE_END):
    """M2 LOWO-CV of the E2 amplitude at kernel ``lam``."""
    return _leave_one_week_out(
        bundles, insample_end,
        lambda bd: fit_global_propagator(bd, lam, insample_end=insample_end))


def cv_global_concave(bundles, insample_end=INSAMPLE_END):
    """M2 LOWO-CV of the E3 g."""
    return _leave_one_week_out(
        bundles, insample_end,
        lambda bd: fit_global_concave(bd, insample_end=insample_end))
