"""Truth-table tests for src/research/bflow/energy_features.py (PURE, no I/O).

T1 of the Phase-1c ENERGY deep-dive. This module is the CONSTRUCTION layer
only (binding spec ``analysis/bflow_phase1c_energy_grid_preenumeration.md`` §1
items 1-5): the window features the grid needs that the harness lacks
(``ofi_30``, ``disp_W``), the E1/E2/E3 energy residuals with their fitted
scalars, and the M2 leave-one-week-out CV of every fitted scalar. The IC grid
(M1), the |r|-tercile split (E4), the sign(r) split (E5) and the two-leg
counterfactual (M3) are DOWNSTREAM tasks and are NOT exercised here.

Every fitted-scalar test uses a synthetic frame with a KNOWN β / kernel
amplitude / concavity-coefficient so the residual is exactly recoverable
(residual ≈ 0 when the construction matches the generating process), and the
GLOBAL causal fit carries an explicit causality invariant: appending a session
dated after the 2026-06-02 in-sample cutoff must NOT change the fitted β.

SIGN CONVENTION (fixed, spec §1): the module reports r; the energy/reversion
hypothesis ⟺ NEGATIVE IC(r → target). T1 does not compute IC, so the sign
convention only matters for downstream consumers; here we test residual
recoverability and causality, which are sign-agnostic.
"""
import math

import numpy as np
import pandas as pd
import pytest

from src.research.bflow import energy_features as ef
from src.research.bflow import flow_features as ff


# --------------------------------------------------------------------------
# builders (mirror tests/test_bflow_flow_features.py conventions)
# --------------------------------------------------------------------------
def _row(minute, c, v=100.0, vw=None, h=None, l=None, o=None):
    """One valid bar row. Defaults: vw=c, h=l=c (spread 0), o=c (minute-0 Δc=0
    unless o overridden)."""
    if vw is None:
        vw = c
    if h is None:
        h = c
    if l is None:
        l = c
    if o is None:
        o = c
    return {"minute": minute, "o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw}


def make_df(rows):
    return pd.DataFrame(rows)


def _full_session(closes, vols=None, o0=None):
    """A full 0..389 session from a list of 390 closes (vw=c, h=l=c). ``o0``
    overrides the minute-0 open (so minute-0 Δc = c0 − o0 can be set)."""
    assert len(closes) == 390
    if vols is None:
        vols = [100.0] * 390
    rows = []
    for m in range(390):
        o = o0 if (m == 0 and o0 is not None) else closes[m]
        rows.append(_row(m, c=closes[m], v=vols[m], o=o))
    return make_df(rows)


# ==========================================================================
# 1. ofi_30 — reuse of ff._ofi with k=30
# ==========================================================================
def test_ofi30_index_and_columns():
    df = make_df([_row(m, c=100.0) for m in range(0, 40)])
    feats = ef.compute_energy_features(df)
    assert list(feats.index) == list(range(0, 390))
    assert feats.index.name == "minute"
    # ofi_30 + disp_15 + disp_30 are the new objects this module adds.
    for col in ("ofi_30", "disp_15", "disp_30"):
        assert col in feats.columns


def test_ofi30_matches_ff_ofi_k30():
    # Build a frame with a non-trivial sign/volume pattern and assert ofi_30
    # equals ff._ofi(work, dc, 30) byte-for-byte (no new definition).
    closes = []
    price = 100.0
    for m in range(390):
        # alternate up/down so signs vary
        price += 1.0 if (m % 2 == 0) else -1.0
        closes.append(price)
    df = _full_session(closes, o0=99.0)
    feats = ef.compute_energy_features(df)

    work, _valid = ff._reindex_valid_frame(df)
    dc = ff._delta_c(work)
    expected = ff._ofi(work, dc, 30)
    # compare on the shared minute index, NaN-aware
    got = feats["ofi_30"]
    assert got.index.equals(expected.index)
    for m in range(390):
        a, b = got.loc[m], expected.loc[m]
        if math.isnan(a) or math.isnan(b):
            assert math.isnan(a) and math.isnan(b), f"minute {m}: {a} vs {b}"
        else:
            assert a == pytest.approx(b), f"minute {m}: {a} vs {b}"


def test_ofi30_nan_before_window_complete():
    # ofi_30 is a rolling sum with min_periods=30 over the sign(Δc)·v series.
    # The window at t covers minutes [t-29..t]; the FIRST window bar's Δc is
    # well-defined for minute 0 (Δc0 = c0 − o0, the special same-bar rule — NOT a
    # reference to a nonexistent minute -1). So the earliest COMPLETE window is
    # t = 29 (minutes 0..29, all finite) and ofi_30 is finite there; t < 29 is
    # NaN (window incomplete). This mirrors ofi_5 being finite at t=4.
    closes = [100.0 + m for m in range(390)]
    df = _full_session(closes, o0=99.0)
    feats = ef.compute_energy_features(df)
    assert math.isnan(feats.loc[27, "ofi_30"])
    assert math.isnan(feats.loc[28, "ofi_30"])   # window incomplete (29 bars)
    assert not math.isnan(feats.loc[29, "ofi_30"])  # 30 bars, minute-0 Δc rule


# ==========================================================================
# 2. disp_W — close-to-close return over the trailing W-min span
# ==========================================================================
def test_disp_W_hand_computed():
    # disp_W_t = c_t / c_{t-W} - 1. With c_m = 100 + m:
    #   disp_15 at t=40: c40=140, c25=125 -> 140/125 - 1 = 0.12
    #   disp_30 at t=40: c40=140, c10=110 -> 140/110 - 1 = 0.272727...
    closes = [100.0 + m for m in range(390)]
    df = _full_session(closes, o0=99.0)
    feats = ef.compute_energy_features(df)
    assert feats.loc[40, "disp_15"] == pytest.approx(140.0 / 125.0 - 1.0)
    assert feats.loc[40, "disp_30"] == pytest.approx(140.0 / 110.0 - 1.0)


def test_disp_W_nan_before_span():
    # disp_15 references c_{t-15}; earliest defined t = 15. disp_30 -> t = 30.
    closes = [100.0 + m for m in range(390)]
    df = _full_session(closes, o0=99.0)
    feats = ef.compute_energy_features(df)
    assert math.isnan(feats.loc[14, "disp_15"])
    assert not math.isnan(feats.loc[15, "disp_15"])
    assert math.isnan(feats.loc[29, "disp_30"])
    assert not math.isnan(feats.loc[30, "disp_30"])


def test_disp_W_endpoint_validity():
    # Endpoint-validity (documented choice b): NaN unless BOTH c_t and c_{t-W}
    # are valid. Invalidate the bar at minute 25 (vw<=0) -> disp_15 at t=40
    # (references c_25) must be NaN; disp_30 at t=40 (references c_10) stays
    # finite.
    closes = [100.0 + m for m in range(390)]
    rows = [_row(m, c=closes[m], o=(99.0 if m == 0 else closes[m]))
            for m in range(390)]
    rows[25]["vw"] = 0.0   # invalid bar -> close NULLed by _reindex_valid_frame
    df = make_df(rows)
    feats = ef.compute_energy_features(df)
    assert math.isnan(feats.loc[40, "disp_15"])      # endpoint c_25 invalid
    assert not math.isnan(feats.loc[40, "disp_30"])  # c_10 + c_40 both valid


def test_disp_W_uses_close_not_vw():
    # Documented choice (a): disp_W is a CLOSE return, NOT the vw-displacement.
    # Make vw diverge from c; disp_W must track c only.
    closes = [100.0 + m for m in range(390)]
    rows = [_row(m, c=closes[m], vw=closes[m] * 2.0,
                 o=(99.0 if m == 0 else closes[m])) for m in range(390)]
    df = make_df(rows)
    feats = ef.compute_energy_features(df)
    # c40/c25 - 1 regardless of vw scaling
    assert feats.loc[40, "disp_15"] == pytest.approx(140.0 / 125.0 - 1.0)


# ==========================================================================
# 3. E1 static linear residual — per-session (lookahead) β mode
# ==========================================================================
def test_e1_per_session_is_labeled_lookahead():
    # The per-session β mode must be exposed under a name that flags it as the
    # lookahead diagnostic, and must reuse the existing pooled-OLS residual.
    assert "lookahead" in ef.E1_PER_SESSION_MODE.lower() or \
           "per_session" in ef.E1_PER_SESSION_MODE.lower()
    assert ef.E1_GLOBAL_MODE != ef.E1_PER_SESSION_MODE


def test_e1_per_session_residual_zero_when_disp_is_affine_in_ofi():
    # Direct (flow, price) synthesis: price = a + b*flow EXACTLY -> per-session
    # OLS residual ≈ 0 on every finite row.
    rng = np.random.default_rng(0)
    flow = pd.Series(rng.normal(size=200))
    a, b = 0.003, 1.7
    price = a + b * flow
    resid = ef.e1_residual_per_session(price, flow)
    assert np.allclose(resid.to_numpy(), 0.0, atol=1e-9)


# ==========================================================================
# 4. E1 GLOBAL CAUSAL β — the new fitter + its causality invariant
# ==========================================================================
def _affine_pair_frame(n, a, b, seed):
    """A labeled per-session bundle: {'flow':Series,'price':Series} where
    price = a + b*flow EXACTLY. Used to drive the global fitter directly on
    pre-extracted (disp_W, OFI_W) pairs so recovery is exact."""
    rng = np.random.default_rng(seed)
    flow = pd.Series(rng.normal(size=n))
    price = a + b * flow
    return {"flow": flow, "price": price}


def test_e1_global_recovers_known_beta_and_intercept():
    # Pool 3 sessions, all generated with the SAME (a,b); the global OLS must
    # return that (a,b) and zero residuals.
    a, b = 0.002, 2.5
    bundles = {
        "2026-05-26": _affine_pair_frame(120, a, b, 1),
        "2026-05-27": _affine_pair_frame(140, a, b, 2),
        "2026-05-28": _affine_pair_frame(160, a, b, 3),
    }
    fit = ef.fit_global_linear(bundles, insample_end="2026-06-02")
    assert fit["b"] == pytest.approx(b, abs=1e-9)
    assert fit["a"] == pytest.approx(a, abs=1e-9)
    # apply the frozen (a,b) -> residuals ~0
    resid = ef.apply_linear_residual(bundles["2026-05-26"]["price"],
                                     bundles["2026-05-26"]["flow"], fit)
    assert np.allclose(resid.dropna().to_numpy(), 0.0, atol=1e-9)


def test_e1_global_filters_to_cutoff_internally():
    # A bundle dated AFTER the cutoff must be IGNORED by the global fit. Give the
    # post-cutoff session a different (a,b); the fit must still match the
    # in-sample (a,b).
    a_in, b_in = 0.001, 1.2
    bundles = {
        "2026-06-01": _affine_pair_frame(100, a_in, b_in, 4),
        "2026-06-02": _affine_pair_frame(100, a_in, b_in, 5),
        # post-cutoff, deliberately different slope/intercept:
        "2026-06-03": _affine_pair_frame(100, 9.9, -7.7, 6),
    }
    fit = ef.fit_global_linear(bundles, insample_end="2026-06-02")
    assert fit["b"] == pytest.approx(b_in, abs=1e-9)
    assert fit["a"] == pytest.approx(a_in, abs=1e-9)


def test_e1_global_causality_invariant_append_post_cutoff():
    # THE causality test: fitting on the ≤06-02 set, then appending a >06-02
    # session, must leave β BYTE-IDENTICAL.
    a, b = 0.0, 3.0
    base = {
        "2026-05-29": _affine_pair_frame(110, a, b, 7),
        "2026-06-02": _affine_pair_frame(110, a, b, 8),
    }
    fit_base = ef.fit_global_linear(base, insample_end="2026-06-02")
    appended = dict(base)
    appended["2026-06-04"] = _affine_pair_frame(110, 50.0, 50.0, 9)  # wild
    fit_app = ef.fit_global_linear(appended, insample_end="2026-06-02")
    assert fit_app["b"] == fit_base["b"]
    assert fit_app["a"] == fit_base["a"]


def test_e1_global_degenerate_zero_variance_flow():
    # Zero-variance pooled flow -> slope undefined -> NaN coeffs (mirrors
    # _pooled_ols_residual's guard).
    flow = pd.Series([1.0] * 50)
    price = pd.Series(np.random.default_rng(0).normal(size=50))
    bundles = {"2026-05-26": {"flow": flow, "price": price}}
    fit = ef.fit_global_linear(bundles, insample_end="2026-06-02")
    assert math.isnan(fit["b"])
    assert math.isnan(fit["a"])


def test_e1_residual_intercept_is_returned_for_downstream():
    # The prereg requires reporting BOTH the with-intercept and no-intercept
    # readings downstream (E4/E5). T1 returns BOTH coeffs so downstream can form
    # either r with no refit. A pure constant shift is Spearman-invariant, so
    # for E1/E2/E3 ICs it is immaterial; assert both keys exist.
    fit = ef.fit_global_linear(
        {"2026-05-26": _affine_pair_frame(80, 0.01, 1.0, 0)},
        insample_end="2026-06-02")
    assert "a" in fit and "b" in fit


# ==========================================================================
# 5. E2 propagator/kernel residual
# ==========================================================================
def test_e2_kernel_decay_shape():
    # G(τ) = exp(-τ/λ). The convolution M_t = Σ_{τ>=0} G(τ)*flow_{t-τ}. With a
    # single unit flow impulse at minute m0 and flow 0 elsewhere, M_{m0+τ} must
    # equal exp(-τ/λ) (causal, only past contributes).
    lam = 15.0
    n = 100
    flow = pd.Series(0.0, index=range(n))
    m0 = 40
    flow.loc[m0] = 1.0
    M = ef.propagator_convolve(flow, lam)
    assert M.loc[m0] == pytest.approx(1.0)            # τ=0 -> exp(0)=1
    assert M.loc[m0 + 5] == pytest.approx(math.exp(-5.0 / lam))
    assert M.loc[m0 + 20] == pytest.approx(math.exp(-20.0 / lam))
    # strictly causal: no contribution before the impulse
    assert M.loc[m0 - 1] == pytest.approx(0.0)


def test_e2_global_recovers_known_amplitude():
    # realized_cum = amplitude * M_t exactly -> global pooled OLS of realized_cum
    # on M recovers the amplitude; residual ~0.
    lam = 5.0
    amp = 3.3
    rng = np.random.default_rng(1)
    bundles = {}
    for i, sess in enumerate(["2026-05-26", "2026-05-27"]):
        flow = pd.Series(rng.normal(size=120), index=range(120))
        M = ef.propagator_convolve(flow, lam)
        realized_cum = amp * M
        bundles[sess] = {"flow": flow, "realized_cum": realized_cum}
    fit = ef.fit_global_propagator(bundles, lam, insample_end="2026-06-02")
    assert fit["amplitude"] == pytest.approx(amp, abs=1e-6)


def test_e2_global_causality_invariant():
    lam = 45.0
    amp = 1.5
    rng = np.random.default_rng(2)

    def _bundle(seed, a):
        flow = pd.Series(np.random.default_rng(seed).normal(size=100),
                         index=range(100))
        M = ef.propagator_convolve(flow, lam)
        return {"flow": flow, "realized_cum": a * M}

    base = {"2026-05-29": _bundle(10, amp), "2026-06-02": _bundle(11, amp)}
    fit_base = ef.fit_global_propagator(base, lam, insample_end="2026-06-02")
    appended = dict(base)
    appended["2026-06-04"] = _bundle(12, 99.0)   # wild post-cutoff amplitude
    fit_app = ef.fit_global_propagator(appended, lam, insample_end="2026-06-02")
    assert fit_app["amplitude"] == fit_base["amplitude"]


def test_e2_realized_cum_pinned_construction():
    # realized_cum_t = c_t / c_{first_valid} - 1, expanding causal window, NaN
    # at/before the first valid bar's reference. With c_m = 100 + m and minute 0
    # valid, first_valid = minute 0, so realized_cum_t = (100+t)/100 - 1 = t/100.
    closes = [100.0 + m for m in range(390)]
    df = _full_session(closes, o0=99.0)
    rc = ef.realized_cum_return(df)
    assert rc.loc[0] == pytest.approx(0.0)
    assert rc.loc[50] == pytest.approx(50.0 / 100.0)
    assert rc.loc[200] == pytest.approx(200.0 / 100.0)


def test_e2_ofi_flow_increment_pinned():
    # The pinned "ofi-flow" per-minute increment = sign(Δc_t)*v_t (the ofi
    # numerator). With c increasing by 1 each minute (sign +1) and v=100,
    # increment = +100 on every minute>=1; minute 0 uses Δc=c0-o0.
    closes = [100.0 + m for m in range(390)]
    df = _full_session(closes, o0=99.0)
    flow = ef.ofi_flow_increment(df)
    assert flow.loc[1] == pytest.approx(100.0)   # sign(+1)*100
    assert flow.loc[5] == pytest.approx(100.0)
    assert flow.loc[0] == pytest.approx(100.0)   # Δc0 = 100-99 = +1 -> +100


# ==========================================================================
# 6. E3 concave impact residual
# ==========================================================================
def test_e3_sqrt_term_shape():
    # The concave regressor = sign(OFI_15)*sqrt(|OFI_15|). Direct synthesis.
    ofi = pd.Series([0.25, -0.25, 0.0, 1.0, -4.0])
    term = ef.concave_term(ofi)
    assert term.loc[0] == pytest.approx(0.5)     # +sqrt(0.25)
    assert term.loc[1] == pytest.approx(-0.5)    # -sqrt(0.25)
    assert term.loc[2] == pytest.approx(0.0)
    assert term.loc[3] == pytest.approx(1.0)     # sqrt(1)
    assert term.loc[4] == pytest.approx(-2.0)    # -sqrt(4)


def test_e3_global_recovers_known_g():
    # disp_15 = a + g * sign(OFI_15)*sqrt(|OFI_15|) exactly -> global OLS
    # recovers g; residual ~0.
    rng = np.random.default_rng(3)
    a, g = 0.001, 0.8
    bundles = {}
    for sess in ["2026-05-26", "2026-05-27"]:
        ofi = pd.Series(rng.uniform(-1.0, 1.0, size=150))
        term = ef.concave_term(ofi)
        disp = a + g * term
        bundles[sess] = {"ofi15": ofi, "disp15": disp}
    fit = ef.fit_global_concave(bundles, insample_end="2026-06-02")
    assert fit["g"] == pytest.approx(g, abs=1e-9)


def test_e3_global_causality_invariant():
    a, g = 0.0, 1.1
    rng = np.random.default_rng(4)

    def _bundle(seed, gg):
        ofi = pd.Series(np.random.default_rng(seed).uniform(-1, 1, size=120))
        term = ef.concave_term(ofi)
        return {"ofi15": ofi, "disp15": gg * term}

    base = {"2026-05-29": _bundle(20, g), "2026-06-02": _bundle(21, g)}
    fit_base = ef.fit_global_concave(base, insample_end="2026-06-02")
    appended = dict(base)
    appended["2026-06-04"] = _bundle(22, 88.0)
    fit_app = ef.fit_global_concave(appended, insample_end="2026-06-02")
    assert fit_app["g"] == fit_base["g"]


# ==========================================================================
# 7. M2 leave-one-week-out CV of every fitted scalar
# ==========================================================================
def test_iso_week_key_from_session():
    # ISO-week key derivation must be deterministic and group dates in the same
    # ISO week together. 2026-06-01 (Mon) and 2026-06-02 (Tue) share an ISO week;
    # 2026-05-26 (Tue) is a different week.
    wk_a = ef.iso_week_key("2026-06-01")
    wk_b = ef.iso_week_key("2026-06-02")
    wk_c = ef.iso_week_key("2026-05-26")
    assert wk_a == wk_b
    assert wk_a != wk_c


def test_m2_linear_returns_insample_and_cv_side_by_side():
    # Build bundles spanning >=2 ISO weeks with a common (a,b). LOWO-CV refits on
    # the other weeks and scores each held-out week; the per-week CV betas should
    # all recover the same b, and the in-sample b is reported beside them.
    a, b = 0.0, 2.0
    bundles = {
        "2026-05-26": _affine_pair_frame(80, a, b, 30),  # ISO wk 22
        "2026-05-27": _affine_pair_frame(80, a, b, 31),  # ISO wk 22
        "2026-06-01": _affine_pair_frame(80, a, b, 32),  # ISO wk 23
        "2026-06-02": _affine_pair_frame(80, a, b, 33),  # ISO wk 23
    }
    res = ef.cv_global_linear(bundles, insample_end="2026-06-02")
    assert res["insample"]["b"] == pytest.approx(b, abs=1e-9)
    # at least 2 held-out weeks, each recovering b
    assert len(res["cv_by_week"]) >= 2
    for wk, fit in res["cv_by_week"].items():
        assert fit["b"] == pytest.approx(b, abs=1e-9)


def test_m2_propagator_cv_recovers_amplitude():
    lam = 15.0
    amp = 2.2
    bundles = {}
    weeks = {"2026-05-26": 40, "2026-05-27": 41, "2026-06-01": 42,
             "2026-06-02": 43}
    for sess, seed in weeks.items():
        flow = pd.Series(np.random.default_rng(seed).normal(size=100),
                         index=range(100))
        M = ef.propagator_convolve(flow, lam)
        bundles[sess] = {"flow": flow, "realized_cum": amp * M}
    res = ef.cv_global_propagator(bundles, lam, insample_end="2026-06-02")
    assert res["insample"]["amplitude"] == pytest.approx(amp, abs=1e-6)
    for wk, fit in res["cv_by_week"].items():
        assert fit["amplitude"] == pytest.approx(amp, abs=1e-6)


def test_m2_concave_cv_recovers_g():
    a, g = 0.0, 0.9
    bundles = {}
    weeks = {"2026-05-26": 50, "2026-05-27": 51, "2026-06-01": 52,
             "2026-06-02": 53}
    for sess, seed in weeks.items():
        ofi = pd.Series(np.random.default_rng(seed).uniform(-1, 1, size=120))
        bundles[sess] = {"ofi15": ofi,
                         "disp15": a + g * ef.concave_term(ofi)}
    res = ef.cv_global_concave(bundles, insample_end="2026-06-02")
    assert res["insample"]["g"] == pytest.approx(g, abs=1e-9)
    for wk, fit in res["cv_by_week"].items():
        assert fit["g"] == pytest.approx(g, abs=1e-9)


# ==========================================================================
# 8. cross-checks against the harness primitives
# ==========================================================================
def test_global_linear_matches_per_session_when_single_session():
    # On a single session the global pooled OLS and the per-session OLS coincide
    # (same rows, same fit). Residuals must match elementwise.
    bundle = _affine_pair_frame(90, 0.004, 1.3, 60)
    # perturb price off-affine so residuals are non-trivial
    rng = np.random.default_rng(61)
    price = bundle["price"] + pd.Series(rng.normal(scale=0.01, size=90))
    flow = bundle["flow"]
    fit = ef.fit_global_linear({"2026-05-26": {"flow": flow, "price": price}},
                               insample_end="2026-06-02")
    r_global = ef.apply_linear_residual(price, flow, fit)
    r_session = ef.e1_residual_per_session(price, flow)
    assert np.allclose(r_global.dropna().to_numpy(),
                       r_session.dropna().to_numpy(), atol=1e-9)


# ==========================================================================
# 9. feature -> fitter integration seam (the one end-to-end path T1 owns)
# ==========================================================================
# T1 builds the construction layer; the per-session, pooled-across-tickers
# BUNDLE assembly (the _session_joint_frames analog) is a DOWNSTREAM driver task
# (Scope items 1-5 do not include frame assembly). The invented bundle contract
# that downstream must conform to is: {session: {'flow': <OFI_W pooled across the
# session's tickers>, 'price': <disp_W pooled, row-aligned>}}. These tests prove
# that compute_energy_features' columns (names, minute index, NaN ranges)
# actually COMPOSE with the fitters when a one-session bundle is hand-assembled
# from a real session frame — catching column-name drift / row-misalignment at
# the feature->fitter seam.
def test_compute_features_feeds_e1_fitter_column_index_seam():
    closes = [100.0 + 0.5 * math.sin(m / 7.0) for m in range(390)]
    df = _full_session(closes, o0=99.5)
    feats = ef.compute_energy_features(df)
    # hand-assemble a single-session E1 bundle from the W=15 columns.
    bundle = {"2026-05-26": {"flow": feats["ofi_15"], "price": feats["disp_15"]}}
    fit = ef.fit_global_linear(bundle, insample_end="2026-06-02")
    assert np.isfinite(fit["a"]) and np.isfinite(fit["b"])
    assert fit["n_sessions"] == 1
    resid = ef.apply_linear_residual(feats["disp_15"], feats["ofi_15"], fit)
    # residual is on the minute index and NaN exactly where either input is NaN.
    assert list(resid.index) == list(range(390))
    both_finite = feats["disp_15"].notna() & feats["ofi_15"].notna()
    assert (resid.notna() == both_finite).all()
    # n_obs equals the both-finite row count.
    assert fit["n_obs"] == int(both_finite.sum())


def test_compute_features_feeds_e2_and_e3_fitters():
    closes = [100.0 + 0.3 * ((m * 7) % 11 - 5) for m in range(390)]
    df = _full_session(closes, o0=99.0)
    feats = ef.compute_energy_features(df)

    # E2: ofi-flow increment + realized-cum, both from the SAME raw frame.
    flow_inc = ef.ofi_flow_increment(df)
    rc = ef.realized_cum_return(df)
    e2_bundle = {"2026-05-26": {"flow": flow_inc, "realized_cum": rc}}
    fit2 = ef.fit_global_propagator(e2_bundle, 15.0, insample_end="2026-06-02")
    assert np.isfinite(fit2["amplitude"])
    r2 = ef.apply_propagator_residual(rc, flow_inc, 15.0, fit2)
    assert list(r2.index) == list(range(390))

    # E3: disp_15 + ofi_15 from the feature frame.
    e3_bundle = {"2026-05-26": {"ofi15": feats["ofi_15"],
                                "disp15": feats["disp_15"]}}
    fit3 = ef.fit_global_concave(e3_bundle, insample_end="2026-06-02")
    assert np.isfinite(fit3["g"])
    r3 = ef.apply_concave_residual(feats["disp_15"], feats["ofi_15"], fit3)
    both_finite = feats["disp_15"].notna() & feats["ofi_15"].notna()
    assert (r3.notna() == both_finite).all()
