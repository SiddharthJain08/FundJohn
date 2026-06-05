"""Truth-table tests for src/research/bflow/flow_features.py (PURE, no I/O).

Every expected value is hand-computed in the comments so the test is a genuine
specification of spec §3 (Phase 1b pre-registration), not a mirror of the
implementation.

Bar contract: a per-(ticker, session) DataFrame with columns
``minute`` (int 0..389 offset from 09:30 ET; gaps possible), o, h, l, c, v, vw.
Windows are over MINUTE INDICES — a missing minute inside a trailing window
makes the feature NaN. A bar is valid iff vw>0 AND v>0 AND h>=l (the oracle.py
``valid_bar`` idiom); an invalid bar poisons its window exactly like a gap.

Features (spec §3, trailing-only, inclusive of t, sign(0)=0, minute 0 uses
Δc = c−o of the same bar):
  ofi_5  = Σ_{i=t-4..t} sign(c_i − c_{i-1})·v_i / Σ_{i=t-4..t} v_i
  ofi_15 = same, 15-min window
  vwap_disp_30 = (c_t − VWAP_{t-29..t}) / VWAP_{t-29..t}, VWAP = Σ v·vw / Σ v

Targets (forward simple returns from c_t):
  ret_to_dump = p_eod_dump/c_t − 1, t ∈ [30, 330]
  ret_fwd_k   = c_{t+k}/c_t − 1, k ∈ {5,15,30,60}, t ∈ [30, 389−k]
"""
import math

import numpy as np
import pandas as pd
import pytest

from src.research.bflow import flow_features as ff


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def _row(minute, c, v=100.0, vw=None, h=None, l=None, o=None):
    """One bar row. Defaults: valid bar (vw=c, h=l=c so spread irrelevant; o=c
    so a minute-0 Δc defaults to 0 unless o is overridden)."""
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


# ==========================================================================
# compute_features — shape / index contract
# ==========================================================================
def test_features_index_and_columns():
    df = make_df([_row(m, c=100.0) for m in range(0, 40)])
    feats = ff.compute_features(df)
    # index is minute 0..389 inclusive
    assert list(feats.index) == list(range(0, 390))
    assert feats.index.name == "minute"
    assert list(feats.columns) == ["ofi_5", "ofi_15", "vwap_disp_30"]


# ==========================================================================
# ofi_5 — hand-computed
# ==========================================================================
def test_ofi5_hand_computed_all_up():
    # minute 0: Δc = c−o. Build o<c so sign=+1 at minute 0 too.
    # prices: 100,101,102,103,104 ; o0 = 99 -> Δc0 = +1 -> sign +1
    rows = [
        _row(0, c=100.0, o=99.0, v=10.0),   # Δc = 100-99 = +1 -> +1*10
        _row(1, c=101.0, v=20.0),           # Δc = 101-100 = +1 -> +1*20
        _row(2, c=102.0, v=30.0),           # +1*30
        _row(3, c=103.0, v=40.0),           # +1*40
        _row(4, c=104.0, v=50.0),           # +1*50
    ]
    feats = ff.compute_features(make_df(rows))
    # t=4 window [0..4]: num = 10+20+30+40+50 = 150 ; den = 150 -> ofi_5 = +1.0
    assert feats.loc[4, "ofi_5"] == pytest.approx(1.0)
    # t<4 -> NaN (window incomplete)
    assert math.isnan(feats.loc[3, "ofi_5"])
    assert math.isnan(feats.loc[0, "ofi_5"])


def test_ofi5_hand_computed_mixed_signs_and_volume_weight():
    # closes: m0=100 (o=100 -> Δc0=0 -> sign 0), m1=101 (+), m2=100 (-),
    #         m3=100 (0), m4=102 (+)
    rows = [
        _row(0, c=100.0, o=100.0, v=10.0),  # Δc0 = 0 -> sign 0 -> 0*10
        _row(1, c=101.0, v=20.0),           # +1*20
        _row(2, c=100.0, v=30.0),           # -1*30
        _row(3, c=100.0, v=40.0),           # 0*40 (flat -> sign 0, vol still counts)
        _row(4, c=102.0, v=50.0),           # +1*50
    ]
    feats = ff.compute_features(make_df(rows))
    # t=4 window [0..4]:
    #   num = 0*10 + 1*20 + (-1)*30 + 0*40 + 1*50 = 20 - 30 + 50 = 40
    #   den = 10+20+30+40+50 = 150
    #   ofi_5 = 40/150
    assert feats.loc[4, "ofi_5"] == pytest.approx(40.0 / 150.0)


def test_ofi5_window_boundary_t_equals_k_minus_1_valid():
    # t=4 is the FIRST minute with a complete 5-window (minutes 0..4).
    rows = [_row(m, c=100.0 + m, o=99.0 if m == 0 else None, v=100.0)
            for m in range(0, 5)]
    feats = ff.compute_features(make_df(rows))
    # all up moves -> ofi_5 = +1 at t=4 ; NaN at t=3 (only 4 bars).
    assert feats.loc[4, "ofi_5"] == pytest.approx(1.0)
    assert math.isnan(feats.loc[3, "ofi_5"])  # t = k-2 -> NaN


# ==========================================================================
# ofi_15 — boundary
# ==========================================================================
def test_ofi15_boundary():
    rows = [_row(m, c=100.0 + m, o=99.0 if m == 0 else None, v=100.0)
            for m in range(0, 16)]
    feats = ff.compute_features(make_df(rows))
    # t=14 first complete 15-window -> all up -> +1 ; t=13 -> NaN
    assert feats.loc[14, "ofi_15"] == pytest.approx(1.0)
    assert math.isnan(feats.loc[13, "ofi_15"])


# ==========================================================================
# sign(0) handling
# ==========================================================================
def test_sign_zero_contributes_zero_numerator_but_volume_to_denominator():
    # all flat closes -> every Δc = 0 -> sign 0 -> numerator 0 -> ofi_5 = 0.
    rows = [_row(m, c=100.0, o=100.0, v=100.0) for m in range(0, 5)]
    feats = ff.compute_features(make_df(rows))
    # den = 500 > 0 ; num = 0 -> 0/500 = 0.0 (NOT NaN — denominator nonzero)
    assert feats.loc[4, "ofi_5"] == pytest.approx(0.0)


# ==========================================================================
# minute-0 c−o convention
# ==========================================================================
def test_minute0_uses_c_minus_o_not_prev_close():
    # minute 0 sign is driven by c0 − o0, with NO reference to any prior bar.
    rows = [
        _row(0, c=100.0, o=105.0, v=10.0),  # Δc0 = 100-105 = -5 -> sign -1
        _row(1, c=100.0, v=10.0),           # Δc1 = 100-100 = 0 -> sign 0
    ]
    feats = ff.compute_features(make_df(rows))
    # we cannot read ofi_5 here (window incomplete), but a single-bar reduction
    # is observable through a degenerate 1-wide check below; instead verify via
    # a 5-window that hinges on the minute-0 sign.
    rows5 = [
        _row(0, c=100.0, o=105.0, v=10.0),  # sign -1 -> -10
        _row(1, c=100.0, v=10.0),           # 0
        _row(2, c=100.0, v=10.0),           # 0
        _row(3, c=100.0, v=10.0),           # 0
        _row(4, c=100.0, v=10.0),           # 0
    ]
    f5 = ff.compute_features(make_df(rows5))
    # num = -10 ; den = 50 -> ofi_5 = -10/50 = -0.2
    assert f5.loc[4, "ofi_5"] == pytest.approx(-0.2)


# ==========================================================================
# invalid-bar propagation (within window -> NaN)
# ==========================================================================
def test_invalid_bar_in_window_makes_ofi5_nan():
    rows = [
        _row(0, c=100.0, o=99.0, v=10.0),
        _row(1, c=101.0, v=20.0),
        _row(2, c=102.0, v=0.0),            # INVALID: v=0
        _row(3, c=103.0, v=40.0),
        _row(4, c=104.0, v=50.0),
    ]
    feats = ff.compute_features(make_df(rows))
    # t=4 window [0..4] contains an invalid bar -> NaN.
    assert math.isnan(feats.loc[4, "ofi_5"])


def test_invalid_vw_bar_in_window_makes_ofi5_nan():
    rows = [
        _row(0, c=100.0, o=99.0, v=10.0),
        _row(1, c=101.0, v=20.0, vw=0.0),   # INVALID: vw=0
        _row(2, c=102.0, v=30.0),
        _row(3, c=103.0, v=40.0),
        _row(4, c=104.0, v=50.0),
    ]
    feats = ff.compute_features(make_df(rows))
    assert math.isnan(feats.loc[4, "ofi_5"])


def test_invalid_bar_h_lt_l_in_window_makes_ofi5_nan():
    rows = [
        _row(0, c=100.0, o=99.0, v=10.0),
        _row(1, c=101.0, v=20.0),
        _row(2, c=102.0, v=30.0, h=101.0, l=103.0),  # INVALID: h<l
        _row(3, c=103.0, v=40.0),
        _row(4, c=104.0, v=50.0),
    ]
    feats = ff.compute_features(make_df(rows))
    assert math.isnan(feats.loc[4, "ofi_5"])


# ==========================================================================
# minute-gap -> NaN
# ==========================================================================
def test_minute_gap_in_window_makes_ofi5_nan():
    # minute 2 is MISSING from the rows -> reindex inserts a NaN row -> window
    # [0..4] incomplete -> NaN.
    rows = [
        _row(0, c=100.0, o=99.0, v=10.0),
        _row(1, c=101.0, v=20.0),
        # minute 2 absent
        _row(3, c=103.0, v=40.0),
        _row(4, c=104.0, v=50.0),
    ]
    feats = ff.compute_features(make_df(rows))
    assert math.isnan(feats.loc[4, "ofi_5"])
    # a window NOT spanning the gap is unaffected: build a long run and check a
    # later complete window. (added below in dedicated test)


def test_window_clear_of_gap_is_computed():
    # gap ONLY at minute 2. With prior-close-matters, the gap poisons every
    # window whose Δc reaches minute 2: windows ending at t=2..6 (they include
    # minute 2) AND the window ending at t=7 (its first bar m=3 needs c2).
    # The first fully clean window starts at minute 4 -> ends at t=8: bars 4..8
    # all valid and c4-c3 is well-defined (c3 valid). Provide minutes 0..12.
    rows = []
    for m in range(0, 13):
        if m == 2:
            continue
        o = 99.0 if m == 0 else None
        rows.append(_row(m, c=100.0 + m, v=100.0, o=o))
    feats = ff.compute_features(make_df(rows))
    # window [3..7]: Δc3 = c3 − c2 (c2 missing) -> NaN.
    assert math.isnan(feats.loc[7, "ofi_5"])
    # window [4..8]: all bars 4..8 valid AND c3 (predecessor of 4) valid;
    # every move is +1 -> ofi_5 = +1.0. THIS is the clean window.
    assert feats.loc[8, "ofi_5"] == pytest.approx(1.0)


# ==========================================================================
# prior-close matters: first window bar's predecessor must be valid
# ==========================================================================
def test_prior_close_validity_matters_for_first_window_bar():
    # window [1..5] all present+valid, but bar 0 INVALID. sign at minute 1 uses
    # c1 − c0 ; c0 is invalid -> Δc1 NaN -> window [1..5] poisoned.
    rows = [
        _row(0, c=100.0, o=99.0, v=0.0),    # INVALID predecessor (v=0)
        _row(1, c=101.0, v=20.0),
        _row(2, c=102.0, v=30.0),
        _row(3, c=103.0, v=40.0),
        _row(4, c=104.0, v=50.0),
        _row(5, c=105.0, v=60.0),
    ]
    feats = ff.compute_features(make_df(rows))
    # window [1..5] requires Δc1 = c1 − c0 ; c0 invalid -> NaN -> ofi_5 NaN.
    assert math.isnan(feats.loc[5, "ofi_5"])


def test_t_equals_k_minus_1_needs_no_predecessor():
    # at t=4 (k=5) the window starts at minute 0, which uses c−o (no
    # predecessor). So a fully-valid 0..4 run yields a real value even though
    # there is no minute -1.
    rows = [_row(m, c=100.0 + m, o=99.0 if m == 0 else None, v=100.0)
            for m in range(0, 5)]
    feats = ff.compute_features(make_df(rows))
    assert feats.loc[4, "ofi_5"] == pytest.approx(1.0)


# ==========================================================================
# vwap_disp_30 — hand-computed
# ==========================================================================
def test_vwap_disp_30_hand_computed():
    # 30 bars minute 0..29 all valid. Use constant vw and v so VWAP = vw.
    # vw = 100 for all, v = 10 for all -> VWAP_{0..29} = 100.
    # c_29 = 101 -> vwap_disp = (101 - 100)/100 = 0.01.
    rows = []
    for m in range(0, 30):
        c = 101.0 if m == 29 else 100.0
        rows.append(_row(m, c=c, o=100.0 if m == 0 else None, v=10.0, vw=100.0,
                         h=c, l=c))
    feats = ff.compute_features(make_df(rows))
    assert feats.loc[29, "vwap_disp_30"] == pytest.approx(0.01)
    # t=28 -> only 29 bars in trailing window -> NaN.
    assert math.isnan(feats.loc[28, "vwap_disp_30"])


def test_vwap_disp_30_volume_weighted():
    # 30 bars. Make vw differ and volume-weight matter.
    # bars 0..28: vw=100, v=1 each (29 bars) ; bar 29: vw=200, v=29.
    # Σ v·vw = 29*100*1 + 200*29 = 2900 + 5800 = 8700
    # Σ v = 29*1 + 29 = 58
    # VWAP = 8700/58 = 150.0
    # c_29 = 160 -> vwap_disp = (160 - 150)/150 = 10/150
    rows = []
    for m in range(0, 29):
        rows.append(_row(m, c=100.0, o=100.0 if m == 0 else None, v=1.0,
                         vw=100.0, h=100.0, l=100.0))
    rows.append(_row(29, c=160.0, v=29.0, vw=200.0, h=160.0, l=160.0))
    feats = ff.compute_features(make_df(rows))
    assert feats.loc[29, "vwap_disp_30"] == pytest.approx(10.0 / 150.0)


def test_vwap_disp_30_nan_on_invalid_bar_in_window():
    rows = []
    for m in range(0, 30):
        v = 0.0 if m == 10 else 10.0   # bar 10 invalid (v=0)
        rows.append(_row(m, c=100.0, o=100.0 if m == 0 else None, v=v, vw=100.0,
                         h=100.0, l=100.0))
    feats = ff.compute_features(make_df(rows))
    assert math.isnan(feats.loc[29, "vwap_disp_30"])


def test_vwap_disp_30_nan_on_gap_in_window():
    rows = []
    for m in range(0, 31):
        if m == 15:
            continue                    # gap at minute 15
        rows.append(_row(m, c=100.0, o=100.0 if m == 0 else None, v=10.0,
                         vw=100.0, h=100.0, l=100.0))
    feats = ff.compute_features(make_df(rows))
    # window [1..30] spans the gap at 15 -> NaN.
    assert math.isnan(feats.loc[30, "vwap_disp_30"])


def test_vwap_disp_30_nan_when_current_bar_invalid():
    # All 30 valid except bar 29 (the current bar) is invalid -> c_29 NaN and
    # window incomplete -> NaN.
    rows = []
    for m in range(0, 30):
        valid = m != 29
        rows.append(_row(m, c=100.0, o=100.0 if m == 0 else None,
                         v=10.0 if valid else 0.0, vw=100.0, h=100.0, l=100.0))
    feats = ff.compute_features(make_df(rows))
    assert math.isnan(feats.loc[29, "vwap_disp_30"])


# ==========================================================================
# NaN sign propagation (not collapse to 0)
# ==========================================================================
def test_nan_delta_does_not_collapse_to_zero_sign():
    # A NaN Δc (from a missing/invalid predecessor) must propagate as NaN, NOT
    # be treated as sign 0 (which would silently zero the numerator term and let
    # the window compute a wrong, non-NaN value). bar 1 invalid inside [1..5].
    rows = [
        _row(0, c=100.0, o=99.0, v=10.0),
        _row(1, c=101.0, v=0.0),            # invalid -> poisons window
        _row(2, c=102.0, v=30.0),
        _row(3, c=103.0, v=40.0),
        _row(4, c=104.0, v=50.0),
        _row(5, c=105.0, v=60.0),
    ]
    feats = ff.compute_features(make_df(rows))
    assert math.isnan(feats.loc[5, "ofi_5"])


# ==========================================================================
# anti-lookahead: features at t never change when bars AFTER t are mutated
# ==========================================================================
def test_features_strictly_trailing_no_lookahead():
    base = [_row(m, c=100.0 + 0.1 * m, o=99.0 if m == 0 else None, v=100.0,
                 vw=100.0 + 0.1 * m, h=100.5 + 0.1 * m, l=99.5 + 0.1 * m)
            for m in range(0, 60)]
    feats_a = ff.compute_features(make_df(base))
    # mutate every bar AFTER minute 30 (prices, vols, vw, range).
    mutated = []
    for r in base:
        r2 = dict(r)
        if r2["minute"] > 30:
            r2["c"] = r2["c"] * 3.0 + 5.0
            r2["vw"] = r2["vw"] * 2.0 + 7.0
            r2["v"] = r2["v"] * 4.0 + 1.0
            r2["h"] = r2["c"] + 1.0
            r2["l"] = r2["c"] - 1.0
        mutated.append(r2)
    feats_b = ff.compute_features(make_df(mutated))
    # every feature value at minutes 0..30 must be byte-identical.
    for col in ("ofi_5", "ofi_15", "vwap_disp_30"):
        a = feats_a.loc[0:30, col]
        b = feats_b.loc[0:30, col]
        # equal_nan: NaNs match NaNs, numbers match exactly.
        assert np.array_equal(a.to_numpy(), b.to_numpy(), equal_nan=True), col


# ==========================================================================
# compute_targets — ret_to_dump
# ==========================================================================
def test_ret_to_dump_hand_computed_and_range():
    rows = [_row(m, c=100.0, o=100.0 if m == 0 else None, v=10.0) for m in range(0, 390)]
    # set a couple of distinct closes for arithmetic
    df = make_df(rows)
    df.loc[df["minute"] == 30, "c"] = 100.0
    df.loc[df["minute"] == 100, "c"] = 125.0
    p_eod = 110.0
    tg = ff.compute_targets(df, p_eod)
    assert list(tg.index) == list(range(0, 390))
    assert tg.index.name == "minute"
    assert "ret_to_dump" in tg.columns
    # t=30 inside [30,330]: 110/100 - 1 = 0.10
    assert tg.loc[30, "ret_to_dump"] == pytest.approx(0.10)
    # t=100: 110/125 - 1
    assert tg.loc[100, "ret_to_dump"] == pytest.approx(110.0 / 125.0 - 1.0)
    # t=29 (just below range) -> NaN ; t=331 (just above) -> NaN
    assert math.isnan(tg.loc[29, "ret_to_dump"])
    assert math.isnan(tg.loc[331, "ret_to_dump"])
    # boundaries inclusive: t=330 valid
    assert not math.isnan(tg.loc[330, "ret_to_dump"])


def test_ret_to_dump_nan_when_bar_invalid():
    rows = [_row(m, c=100.0, o=100.0 if m == 0 else None, v=10.0) for m in range(0, 390)]
    df = make_df(rows)
    # invalidate minute 50 (in range)
    df.loc[df["minute"] == 50, "v"] = 0.0
    tg = ff.compute_targets(df, 110.0)
    assert math.isnan(tg.loc[50, "ret_to_dump"])


def test_ret_to_dump_nan_when_p_eod_none():
    rows = [_row(m, c=100.0, o=100.0 if m == 0 else None, v=10.0) for m in range(0, 390)]
    tg = ff.compute_targets(make_df(rows), None)
    assert tg["ret_to_dump"].isna().all()


def test_ret_to_dump_nan_on_missing_minute_in_range():
    rows = [_row(m, c=100.0, o=100.0 if m == 0 else None, v=10.0)
            for m in range(0, 390) if m != 60]
    tg = ff.compute_targets(make_df(rows), 110.0)
    assert math.isnan(tg.loc[60, "ret_to_dump"])


# ==========================================================================
# compute_targets — ret_fwd_k
# ==========================================================================
def test_ret_fwd_k_hand_computed():
    rows = [_row(m, c=100.0 + m, o=99.0 if m == 0 else None, v=10.0)
            for m in range(0, 390)]
    tg = ff.compute_targets(make_df(rows), 200.0)
    assert "ret_fwd_5" in tg.columns
    # t=30: c=130, c_{35}=135 -> 135/130 - 1
    assert tg.loc[30, "ret_fwd_5"] == pytest.approx(135.0 / 130.0 - 1.0)
    # t=30, k=60: c_{90}=190 -> 190/130 - 1
    assert tg.loc[30, "ret_fwd_60"] == pytest.approx(190.0 / 130.0 - 1.0)


def test_ret_fwd_k_range_boundaries():
    rows = [_row(m, c=100.0 + m, o=99.0 if m == 0 else None, v=10.0)
            for m in range(0, 390)]
    tg = ff.compute_targets(make_df(rows), 200.0)
    # k=5: t ∈ [30, 384]. t=29 -> NaN ; t=384 -> valid ; t=385 -> NaN.
    assert math.isnan(tg.loc[29, "ret_fwd_5"])
    assert not math.isnan(tg.loc[384, "ret_fwd_5"])
    assert math.isnan(tg.loc[385, "ret_fwd_5"])
    # k=60: t ∈ [30, 329]. t=329 valid ; t=330 -> NaN.
    assert not math.isnan(tg.loc[329, "ret_fwd_60"])
    assert math.isnan(tg.loc[330, "ret_fwd_60"])


def test_ret_fwd_k_nan_when_endpoint_bar_invalid():
    rows = [_row(m, c=100.0 + m, o=99.0 if m == 0 else None, v=10.0)
            for m in range(0, 390)]
    df = make_df(rows)
    # invalidate the forward endpoint for t=100,k=5 -> minute 105 invalid.
    df.loc[df["minute"] == 105, "v"] = 0.0
    tg = ff.compute_targets(df, 200.0)
    assert math.isnan(tg.loc[100, "ret_fwd_5"])
    # the base bar t=100 itself is fine for k=15 (endpoint 115 valid).
    assert not math.isnan(tg.loc[100, "ret_fwd_15"])


def test_ret_fwd_k_nan_when_base_bar_invalid():
    rows = [_row(m, c=100.0 + m, o=99.0 if m == 0 else None, v=10.0)
            for m in range(0, 390)]
    df = make_df(rows)
    df.loc[df["minute"] == 100, "v"] = 0.0   # base bar invalid
    tg = ff.compute_targets(df, 200.0)
    assert math.isnan(tg.loc[100, "ret_fwd_5"])


def test_ret_fwd_k_nan_when_endpoint_missing():
    rows = [_row(m, c=100.0 + m, o=99.0 if m == 0 else None, v=10.0)
            for m in range(0, 390) if m != 135]
    tg = ff.compute_targets(make_df(rows), 200.0)
    # t=120, k=15 -> endpoint 135 missing -> NaN.
    assert math.isnan(tg.loc[120, "ret_fwd_15"])


def test_targets_all_columns_present():
    rows = [_row(m, c=100.0 + m, o=99.0 if m == 0 else None, v=10.0)
            for m in range(0, 390)]
    tg = ff.compute_targets(make_df(rows), 200.0)
    expected = ["ret_to_dump", "ret_fwd_5", "ret_fwd_15", "ret_fwd_30", "ret_fwd_60"]
    assert list(tg.columns) == expected


# ==========================================================================
# dedup: duplicate minute rows -> last VALID wins (mirrors oracle.window_stats)
# ==========================================================================
def test_duplicate_minute_last_valid_wins_among_valids():
    rows = [
        _row(0, c=100.0, o=99.0, v=10.0),
        _row(1, c=101.0, v=20.0),
        _row(2, c=102.0, v=30.0),
        _row(3, c=103.0, v=40.0),
        _row(4, c=104.0, v=50.0),
        _row(4, c=200.0, v=999.0),   # duplicate minute 4 (both valid) -> last wins
    ]
    feats = ff.compute_features(make_df(rows))
    # with the LAST valid minute-4 row (c=200, up vs c3=103, v=999):
    #   num = 10 (m0 up) + 20 + 30 + 40 + 999 (all up) = 1099
    #   den = 10 + 20 + 30 + 40 + 999 = 1099 -> ofi_5 = +1.0
    assert feats.loc[4, "ofi_5"] == pytest.approx(1.0)


def test_duplicate_minute_valid_then_invalid_keeps_valid():
    # Discriminating case: minute 4 appears VALID then INVALID. oracle keeps the
    # valid bar (a later invalid duplicate never overwrites it). So the window
    # [0..4] must use the VALID minute-4 row and yield a real value, NOT NaN.
    rows = [
        _row(0, c=100.0, o=99.0, v=10.0),
        _row(1, c=101.0, v=20.0),
        _row(2, c=102.0, v=30.0),
        _row(3, c=103.0, v=40.0),
        _row(4, c=104.0, v=50.0),            # VALID minute 4
        _row(4, c=104.0, v=0.0),             # INVALID duplicate (v=0) -> ignored
    ]
    feats = ff.compute_features(make_df(rows))
    # valid minute-4 row stands: all up moves -> ofi_5 = +1.0 (NOT NaN).
    assert feats.loc[4, "ofi_5"] == pytest.approx(1.0)
