"""Tests for src/research/bflow/predictability.py — Test A of the Phase-1b
pre-registration (the GATING predictive-structure test).

Spec §3: per-session pooled Spearman rank IC across all valid (ticker, minute)
observations, then across-session mean IC and t = mean(IC)/(sd(IC)/√n_sessions).
Session = the cluster; across-session t ONLY, never pooled SEs.

The IC machinery (Spearman rank correlation, the across-session t, the cell
grid) is exercised here on directly-constructed (x, y) arrays / synthetic grids,
NOT driven through the bar-level feature math — that is the job of
test_bflow_flow_features. ``session_ics`` (the only function that touches bars)
gets focused integration tests: wiring, the Phase-1 60-valid-bar floor, the
30-obs cell floor, and finite ICs on a complete synthetic session.

Cells = every (feature ∈ {ofi_5, ofi_15, vwap_disp_30}) × (target ∈
{ret_to_dump, ret_fwd_5, ret_fwd_15, ret_fwd_30, ret_fwd_60}) = 15 cells.
"""
import math

import numpy as np
import pandas as pd
import pytest

from src.research.bflow import predictability as pr


# --------------------------------------------------------------------------
# module-level constants / cell taxonomy
# --------------------------------------------------------------------------
def test_no_spec_absent_cell_observation_floor():
    # Spec §3 fixes NO per-session-cell minimum-observation gate — it says
    # "all valid (ticker, minute) observations in the session -> one IC per
    # session per cell". A spec-absent floor would drop thin cells from the
    # non-NaN n_sessions count and thereby perturb the GATING across-session t,
    # so it MUST NOT exist. Regression guard: the constant is gone.
    assert not hasattr(pr, "MIN_OBS_PER_SESSION_CELL")


def test_cells_are_the_15_feature_x_target_product():
    # 3 features x 5 targets, fixed order.
    feats = ["ofi_5", "ofi_15", "vwap_disp_30"]
    targets = ["ret_to_dump", "ret_fwd_5", "ret_fwd_15", "ret_fwd_30",
               "ret_fwd_60"]
    expected = [pr.cell_name(f, t) for f in feats for t in targets]
    assert list(pr.CELLS) == expected
    assert len(pr.CELLS) == 15
    # delimiter must not collide with the underscores already in the names
    assert pr.cell_name("ofi_5", "ret_to_dump") != pr.cell_name("ofi", "5_ret_to_dump")


# ==========================================================================
# _spearman_ic — pure rank-IC helper
# ==========================================================================
def test_spearman_perfect_monotone_is_plus_one():
    x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0] * 8)   # 40 obs, no ties within block
    # strictly increasing distinct values so no ties: use arange
    x = pd.Series(np.arange(40, dtype=float))
    y = pd.Series(np.arange(40, dtype=float) * 3.0 + 7.0)  # affine increasing
    assert pr._spearman_ic(x, y) == pytest.approx(1.0)


def test_spearman_perfect_antitone_is_minus_one():
    x = pd.Series(np.arange(40, dtype=float))
    y = pd.Series(-np.arange(40, dtype=float))
    assert pr._spearman_ic(x, y) == pytest.approx(-1.0)


def test_spearman_distinguishes_from_pearson_on_monotone_nonlinear():
    # Strictly monotone but heavily nonlinear (cube of distinct values): Spearman
    # IC must be exactly 1.0 (rank-preserving) while Pearson would be < 1.
    x = pd.Series(np.arange(1, 41, dtype=float))     # distinct -> no ties
    y = x ** 3
    ic = pr._spearman_ic(x, y)
    assert ic == pytest.approx(1.0)
    # prove it is NOT plain Pearson: Pearson(x, x**3) is strictly below 1.
    pearson = np.corrcoef(x.to_numpy(), y.to_numpy())[0, 1]
    assert pearson < 0.99


def test_spearman_drops_nan_pairs_then_correlates():
    # 40 clean monotone pairs + a handful of NaN-poisoned pairs that must be
    # dropped (NOT counted, NOT zero-filled).
    x = list(np.arange(40, dtype=float))
    y = list(np.arange(40, dtype=float))
    # inject NaNs on either side
    x += [np.nan, 5.0, np.nan]
    y += [1.0, np.nan, np.nan]
    ic = pr._spearman_ic(pd.Series(x), pd.Series(y))
    assert ic == pytest.approx(1.0)


def test_spearman_small_sample_no_floor():
    # No spec-absent observation floor: a thin-but-valid sample (well under the
    # removed 30-obs gate) MUST compute its IC, not return NaN. 5 clean monotone
    # pairs -> +1.0; 3 -> +1.0. Only the intrinsic guards (n<2 / zero-variance /
    # zero-denominator) may NaN a cell.
    x5 = pd.Series(np.arange(5, dtype=float))
    y5 = pd.Series(np.arange(5, dtype=float))
    assert pr._spearman_ic(x5, y5) == pytest.approx(1.0)
    x3 = pd.Series([0.0, 1.0, 2.0])
    y3 = pd.Series([10.0, 20.0, 30.0])
    assert pr._spearman_ic(x3, y3) == pytest.approx(1.0)


def test_spearman_intrinsic_guards_nan_below_two_obs():
    # The ONLY intrinsically-required guards survive the floor removal: a single
    # pair (n=1, zero-variance ranks) and an empty pair set (n=0) are undefined
    # correlations -> NaN, never inf / never a RuntimeWarning.
    one_x = pd.Series([3.0])
    one_y = pd.Series([7.0])
    assert math.isnan(pr._spearman_ic(one_x, one_y))
    empty = pd.Series(dtype=float)
    assert math.isnan(pr._spearman_ic(empty, empty))


def test_spearman_constant_vector_is_nan():
    # Zero-variance ranked vector -> correlation undefined -> NaN (no warning).
    x = pd.Series([3.0] * 40)
    y = pd.Series(np.arange(40, dtype=float))
    assert math.isnan(pr._spearman_ic(x, y))
    assert math.isnan(pr._spearman_ic(y, x))


# ==========================================================================
# ic_grid — sessions x cells assembly
# ==========================================================================
def test_ic_grid_shape_and_fixed_columns():
    per = [
        ("2026-04-13", {c: 0.1 for c in pr.CELLS}),
        ("2026-04-14", {c: -0.2 for c in pr.CELLS}),
    ]
    grid = pr.ic_grid(per)
    assert list(grid.columns) == list(pr.CELLS)
    assert list(grid.index) == ["2026-04-13", "2026-04-14"]
    assert grid.loc["2026-04-13", pr.CELLS[0]] == pytest.approx(0.1)


def test_ic_grid_missing_cell_key_becomes_nan():
    # a session dict missing a cell -> that grid entry NaN (still a full column).
    partial = {c: 0.3 for c in pr.CELLS}
    dropped = pr.CELLS[0]
    del partial[dropped]
    grid = pr.ic_grid([("s1", partial)])
    assert math.isnan(grid.loc["s1", dropped])
    assert grid.loc["s1", pr.CELLS[1]] == pytest.approx(0.3)


# ==========================================================================
# summarize — across-session mean / sd / n / t
# ==========================================================================
def test_summarize_columns_and_index():
    per = [(f"s{i}", {c: 0.1 for c in pr.CELLS}) for i in range(5)]
    grid = pr.ic_grid(per)
    summ = pr.summarize(grid)
    assert list(summ.index) == list(pr.CELLS)
    assert set(summ.columns) == {"mean_ic", "sd_ic", "n_sessions", "t"}


def test_summarize_hand_computed_mean_sd_t():
    # one cell, hand-pick 4 ICs: 0.1, 0.2, 0.3, 0.4
    cell = pr.CELLS[0]
    vals = [0.1, 0.2, 0.3, 0.4]
    per = [(f"s{i}", {cell: v}) for i, v in enumerate(vals)]
    grid = pr.ic_grid(per)
    summ = pr.summarize(grid)
    mean = sum(vals) / 4                              # 0.25
    # sample sd (ddof=1)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / 3)
    t = mean / (sd / math.sqrt(4))
    assert summ.loc[cell, "mean_ic"] == pytest.approx(mean)
    assert summ.loc[cell, "sd_ic"] == pytest.approx(sd)
    assert summ.loc[cell, "n_sessions"] == 4
    assert summ.loc[cell, "t"] == pytest.approx(t)


def test_summarize_excludes_nan_sessions_from_n():
    cell = pr.CELLS[0]
    per = [
        ("s0", {cell: 0.2}),
        ("s1", {cell: np.nan}),    # NaN session-cell -> excluded from n
        ("s2", {cell: 0.4}),
        ("s3", {cell: np.nan}),
    ]
    grid = pr.ic_grid(per)
    summ = pr.summarize(grid)
    assert summ.loc[cell, "n_sessions"] == 2
    assert summ.loc[cell, "mean_ic"] == pytest.approx(0.3)


def test_summarize_single_session_t_is_nan():
    # n=1 -> sd undefined (ddof=1) -> t NaN, never inf/raise.
    cell = pr.CELLS[0]
    grid = pr.ic_grid([("s0", {cell: 0.5})])
    summ = pr.summarize(grid)
    assert summ.loc[cell, "n_sessions"] == 1
    assert math.isnan(summ.loc[cell, "t"])


# ==========================================================================
# planted-signal recovery — feature NEGATIVELY predicts target, many sessions
# ==========================================================================
def test_planted_negative_signal_recovers_negative_ic_large_t():
    # Build the IC grid directly (one cell) from many fake sessions where the
    # feature is constructed to anti-correlate with the target. Each session's
    # pooled IC must be strongly negative; across-session mean IC < 0 with large
    # |t| (coherent negative sign).
    rng = np.random.default_rng(12345)
    cell = pr.CELLS[0]
    per = []
    for s in range(30):
        x = rng.normal(size=200)
        # target = -2*x + small noise -> Spearman IC ~ -1
        y = -2.0 * x + 0.05 * rng.normal(size=200)
        ic = pr._spearman_ic(pd.Series(x), pd.Series(y))
        per.append((f"s{s}", {cell: ic}))
    grid = pr.ic_grid(per)
    summ = pr.summarize(grid)
    assert summ.loc[cell, "mean_ic"] < -0.5
    assert summ.loc[cell, "t"] < -10          # very large |t|, negative sign


# ==========================================================================
# pure-noise null — seeded; |t| < 2 typically (loose assert)
# ==========================================================================
def test_noise_null_t_below_two():
    # ONE cell, fixed seed verified to pass. 15 independent nulls would false-
    # positive ~50% of the time, so we test a single constructed cell.
    rng = np.random.default_rng(20260605)
    cell = pr.CELLS[0]
    per = []
    for s in range(40):
        x = rng.normal(size=150)
        y = rng.normal(size=150)               # independent noise
        ic = pr._spearman_ic(pd.Series(x), pd.Series(y))
        per.append((f"s{s}", {cell: ic}))
    grid = pr.ic_grid(per)
    summ = pr.summarize(grid)
    assert abs(summ.loc[cell, "t"]) < 2.0


# ==========================================================================
# session_ics — integration over bars (wiring, floors)
# ==========================================================================
def _bar(minute, c, v=100.0, vw=None, h=None, l=None, o=None):
    if vw is None:
        vw = c
    if h is None:
        h = c
    if l is None:
        l = c
    if o is None:
        o = c
    return {"minute": minute, "o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw}


def _full_session_df(seed, n=390):
    """A complete (no-gap) synthetic session DataFrame, 390 valid bars, with a
    monotone random-walk price so features/targets are well-defined and varied."""
    rng = np.random.default_rng(seed)
    price = 100.0 + np.cumsum(rng.normal(scale=0.1, size=n))
    rows = []
    prev = 100.0
    for m in range(n):
        c = float(price[m])
        o = prev if m > 0 else c - 0.05
        rows.append(_bar(m, c=c, v=float(100 + rng.integers(0, 50)),
                         vw=c, h=c + 0.01, l=c - 0.01, o=o))
        prev = c
    return pd.DataFrame(rows)


def test_session_ics_returns_all_15_cells():
    df_a = _full_session_df(1)
    df_b = _full_session_df(2)
    session_bars = {"AAA": df_a, "BBB": df_b}
    dump = {"AAA": 101.0, "BBB": 99.0}
    ics = pr.session_ics(session_bars, dump)
    assert set(ics.keys()) == set(pr.CELLS)
    # complete sessions with two pooled tickers -> plenty of obs -> finite ICs
    finite = [c for c in pr.CELLS if not math.isnan(ics[c])]
    assert len(finite) == 15


def test_session_ics_60_valid_bar_floor_excludes_thin_ticker():
    # GOOD ticker: full 390-bar session. THIN ticker: only 50 valid bars
    # (< Phase-1 60 floor) -> contributes to NO cell. With only the thin ticker,
    # every cell is NaN (no qualifying observations).
    thin_rows = [_bar(m, c=100.0 + 0.1 * m) for m in range(50)]
    thin = pd.DataFrame(thin_rows)
    ics = pr.session_ics({"THIN": thin}, {"THIN": 105.0})
    assert all(math.isnan(ics[c]) for c in pr.CELLS)


def test_session_ics_thin_ticker_does_not_pool_with_good():
    # The thin ticker's rows must be entirely dropped; the result must equal the
    # good ticker alone.
    df_good = _full_session_df(7)
    thin = pd.DataFrame([_bar(m, c=100.0 + 0.1 * m) for m in range(50)])
    dump = {"GOOD": 100.5, "THIN": 105.0}
    ics_both = pr.session_ics({"GOOD": df_good, "THIN": thin}, dump)
    ics_good = pr.session_ics({"GOOD": df_good}, {"GOOD": 100.5})
    for c in pr.CELLS:
        a, b = ics_both[c], ics_good[c]
        assert (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b)


def test_session_ics_all_nan_target_cell_is_nan():
    # A single full ticker: ret_to_dump pools minutes 30..330 (301 obs) -> a
    # vwap_disp_30 x ret_to_dump cell is defined. But construct a scenario where
    # a target pools ZERO finite pairs by making the dump price NaN for the ONLY
    # ticker: ret_to_dump becomes all-NaN -> every *xret_to_dump cell NaN (the
    # intrinsic zero-pairs/zero-variance guard), while ret_fwd_* cells stay
    # defined. (This is the empty-pool path, NOT a removed observation floor.)
    df = _full_session_df(11)
    ics = pr.session_ics({"AAA": df}, {"AAA": float("nan")})
    rtd_cells = [pr.cell_name(f, "ret_to_dump")
                 for f in ("ofi_5", "ofi_15", "vwap_disp_30")]
    for c in rtd_cells:
        assert math.isnan(ics[c]), c
    # ret_fwd cells unaffected by the missing dump price
    fwd_cell = pr.cell_name("ofi_5", "ret_fwd_5")
    assert not math.isnan(ics[fwd_cell])


def test_session_ics_missing_dump_only_nans_ret_to_dump():
    # Dump price present for one ticker, absent for another. The present ticker's
    # ret_to_dump still pools; ret_fwd_* unaffected for both. Net: all 15 cells
    # finite (two full tickers, one with a dump price).
    df_a = _full_session_df(3)
    df_b = _full_session_df(4)
    ics = pr.session_ics({"AAA": df_a, "BBB": df_b},
                         {"AAA": 100.7})        # BBB dump missing
    assert not math.isnan(ics[pr.cell_name("ofi_5", "ret_to_dump")])
    assert not math.isnan(ics[pr.cell_name("ofi_5", "ret_fwd_30")])
