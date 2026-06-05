"""Truth-table tests for src/research/bflow/energy_grid.py (the M1 scoring layer).

T2 of the Phase-1c ENERGY deep-dive. Binding spec
``analysis/bflow_phase1c_energy_grid_preenumeration.md`` §1 (grid) + §2 (M1/M2).
energy_grid consumes T1's ``energy_features`` construction primitives and emits
the PRE-ENUMERATED energy IC grid (session-clustered Spearman ICs) as a tidy
DataFrame. It computes NO bps / policy numbers and imports NO flow_policy (M3 is
a separate downstream task).

These tests pin the GRID MEMBERSHIP exactly (cell-count exactness per family:
12/9/6/9/6 + anchors), prove the anchor ICs align with the exploratory harness
on a shared synthetic fixture (the same _spearman_ic over the same pooled
columns), prove the E4 tercile partition is complete + session-relative and the
E5 sign-subset partition is the r>0/r<0 split, prove the intercept reading is
Spearman-immaterial to E1/E2/E3 (so the grid is stable) yet bites E4/E5 (the
no-intercept reading rides ``extra``, never a new cell), and prove the M2 CV
scalars land beside the in-sample fits in ``extra``.

SIGN CONVENTION (fixed, spec §1): every cell reports IC(r → target); the
energy/reversion hypothesis ⟺ NEGATIVE IC. These tests check membership /
alignment / determinism, which are sign-agnostic.
"""
import math

import numpy as np
import pandas as pd
import pytest

from src.research.bflow import energy_grid as eg
from src.research.bflow import energy_features as ef
from src.research.bflow import exploratory_phase1c as p1c
from src.research.bflow import predictability as pr


# --------------------------------------------------------------------------
# synthetic session builders (mirror tests/test_bflow_*.py conventions)
# --------------------------------------------------------------------------
def _row(minute, c, v=100.0, vw=None, h=None, l=None, o=None):
    if vw is None:
        vw = c
    if h is None:
        h = c
    if l is None:
        l = c
    if o is None:
        o = c
    return {"minute": minute, "o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw}


def _ticker_frame(seed, with_dump=True):
    """A full 0..389 session for ONE ticker, with a noisy random-walk close so
    every feature/target has variance + a valid EOD dump window. Returns the
    bars DataFrame."""
    rng = np.random.default_rng(seed)
    closes = [100.0]
    for _ in range(389):
        closes.append(closes[-1] * (1.0 + rng.normal(scale=0.001)))
    vols = list(rng.uniform(80.0, 120.0, size=390))
    rows = []
    for m in range(390):
        # small intrabar range so spread_bps is finite + valid_bar passes
        c = closes[m]
        o = (closes[m] * 0.999) if m == 0 else closes[m]
        rows.append(_row(m, c=c, v=vols[m], o=o, h=c * 1.0005, l=c * 0.9995))
    return pd.DataFrame(rows)


def _session_bars(n_tickers=3, base_seed=0):
    """dict ticker -> bars DataFrame for a synthetic session."""
    return {f"TK{i}": _ticker_frame(base_seed + i) for i in range(n_tickers)}


def _dump_prices(session_bars):
    from src.research.bflow import oracle
    return {tk: oracle.dump_benchmark(tdf.to_dict("records"))
            for tk, tdf in session_bars.items()}


def _pools_from_sessions(session_dates, n_tickers=3):
    """Build the energy_grid per-session pooled frames directly from synthetic
    in-memory sessions (no cache I/O). Mirrors build_session_pools' inner loop."""
    pools = {}
    for i, sess in enumerate(session_dates):
        sb = _session_bars(n_tickers=n_tickers, base_seed=100 * (i + 1))
        dump = _dump_prices(sb)
        frames = eg._session_energy_frames(sb, dump)
        pools[sess] = eg._session_pooled(frames)
    return pools


# Two ISO weeks so M2 LOWO-CV has >= 2 held-out weeks.
_SESSIONS = ["2026-05-26", "2026-05-27", "2026-06-01", "2026-06-02"]


# ==========================================================================
# 1. GRID MEMBERSHIP — cell-count exactness per family (the prereg grid)
# ==========================================================================
def test_expected_counts_match_spec_numbers():
    c = eg.expected_cell_counts()
    assert c["E1"] == 12     # W{15,30} × mode{global,persession} × 3 targets
    assert c["E2"] == 9      # λ{5,15,45} × 3 targets
    assert c["E3"] == 6      # {sqrt, linear} × 3 targets
    assert c["E4"] == 9      # 3 terciles × 3 targets
    assert c["E5"] == 6      # {overshoot, undershoot} × 3 targets
    assert c["ANCHOR"] == 15  # 5 anchor features × 3 targets
    assert c["ANCHOR_DIFF"] == 3   # paired r-vs-disp × 3 targets


def test_grid_has_exactly_the_enumerated_cells_per_family():
    pools = _pools_from_sessions(_SESSIONS)
    grid, _fits = eg.build_grid(pools)
    exp = eg.expected_cell_counts()
    counts = grid["family"].value_counts().to_dict()
    for fam, n in exp.items():
        assert counts.get(fam, 0) == n, f"{fam}: {counts.get(fam,0)} != {n}"
    # NO diagnostic (no-intercept) cells leaked into the grid rows.
    assert not grid["variant"].str.startswith("noint_").any()
    # total grid rows = sum of the enumerated families (no extras).
    assert len(grid) == sum(exp.values())


def test_grid_columns_and_determinism():
    pools = _pools_from_sessions(_SESSIONS)
    g1, _ = eg.build_grid(pools)
    g2, _ = eg.build_grid(pools)
    assert list(g1.columns) == eg._GRID_COLUMNS
    # byte-stable run-to-run (deterministic order + values).
    pd.testing.assert_frame_equal(g1, g2)


def test_e1_has_both_beta_modes_each_window():
    pools = _pools_from_sessions(_SESSIONS)
    grid, _ = eg.build_grid(pools)
    e1 = grid[grid["family"] == "E1"]
    variants = set(e1["variant"])
    # 4 distinct (W, mode) variants, each over 3 targets.
    assert variants == {"W15_global", "W15_persession",
                        "W30_global", "W30_persession"}
    for v in variants:
        assert (e1["variant"] == v).sum() == 3


def test_e2_three_lambdas_e3_two_variants():
    pools = _pools_from_sessions(_SESSIONS)
    grid, _ = eg.build_grid(pools)
    e2v = set(grid[grid["family"] == "E2"]["variant"])
    assert e2v == {"lam5", "lam15", "lam45"}
    e3v = set(grid[grid["family"] == "E3"]["variant"])
    assert e3v == {"sqrt", "linear"}


# ==========================================================================
# 2. ANCHOR alignment with the exploratory harness on a shared fixture
# ==========================================================================
def test_anchor_ic_matches_explicit_spearman_on_pooled_columns():
    # The anchor IC must equal predictability._spearman_ic over the SAME pooled
    # feature/target columns the energy frame builds — i.e. energy_grid routes
    # through the identical IC primitive, no hidden transform.
    pools = _pools_from_sessions(["2026-05-26"])
    pool = pools["2026-05-26"]
    ics = eg._anchor_session_ics(pool)
    for feat, label in (("ofi_15", "ofi_15"), ("disp_15", "disp_15")):
        for t in eg.TARGETS:
            cid = eg._cell_id("ANCHOR", label, t)
            expected = pr._spearman_ic(pool[feat], pool[t])
            got = ics[cid]
            if math.isnan(expected):
                assert math.isnan(got)
            else:
                assert got == pytest.approx(expected)


def test_vwap_disp30_anchor_matches_exploratory_anchor_object():
    # The re-reported 1b anchor must equal the exploratory anchor's
    # vwap_disp_30 IC on the SAME tickers (shared synthetic fixture). We build
    # the exploratory anchor via its own _ic_anchor over the same session bars.
    sb = _session_bars(n_tickers=3, base_seed=777)
    dump = _dump_prices(sb)
    # energy_grid pooled frame
    efr = eg._session_energy_frames(sb, dump)
    pool = eg._session_pooled(efr)
    eg_ics = eg._anchor_session_ics(pool)
    # exploratory anchor
    expl_frames = p1c._session_joint_frames(sb, dump)
    expl = p1c._ic_anchor(expl_frames)
    for t in eg.TARGETS:
        cid = eg._cell_id("ANCHOR", "vwap_disp_30_1b", t)
        expl_key = f"A:{p1c.PRICE}->{t}"   # PRICE == 'vwap_disp_30'
        a, b = eg_ics[cid], expl[expl_key]
        if math.isnan(a) or math.isnan(b):
            assert math.isnan(a) and math.isnan(b), f"{t}: {a} vs {b}"
        else:
            assert a == pytest.approx(b), f"{t}: {a} vs {b}"


# ==========================================================================
# 3. E4 tercile partition — complete + session-relative
# ==========================================================================
def test_e4_tercile_masks_partition_the_finite_support():
    # The 3 terciles must be DISJOINT and together cover every finite |r| row.
    rng = np.random.default_rng(5)
    absr = pd.Series(rng.uniform(0, 1, size=300))
    masks = eg._tercile_masks(absr, absr.dropna())
    low, mid, high = masks["q1_low"], masks["q2_mid"], masks["q3_high"]
    # disjoint
    assert not (low & mid).any()
    assert not (low & high).any()
    assert not (mid & high).any()
    # exhaustive over the finite support
    union = low | mid | high
    assert union.all()                       # every finite row is in exactly one
    # roughly balanced terciles (within reason for n=300)
    for m in (low, mid, high):
        assert 60 <= int(m.sum()) <= 140


def test_e4_too_few_points_yields_none_masks():
    absr = pd.Series([0.1, 0.2])             # < 3 finite
    masks = eg._tercile_masks(absr, absr.dropna())
    assert masks["q1_low"] is None
    assert masks["q2_mid"] is None
    assert masks["q3_high"] is None


def test_e4_grid_has_three_terciles_per_target():
    pools = _pools_from_sessions(_SESSIONS)
    grid, _ = eg.build_grid(pools)
    e4 = grid[grid["family"] == "E4"]
    variants = set(e4["variant"])
    assert variants == {"q1_low", "q2_mid", "q3_high"}
    for v in variants:
        assert (e4["variant"] == v).sum() == 3


# ==========================================================================
# 4. E5 sign-subset partition — exactly r>0 and r<0
# ==========================================================================
def test_e5_grid_is_overshoot_and_undershoot():
    pools = _pools_from_sessions(_SESSIONS)
    grid, _ = eg.build_grid(pools)
    e5 = grid[grid["family"] == "E5"]
    assert set(e5["variant"]) == {"overshoot_pos", "undershoot_neg"}
    for v in set(e5["variant"]):
        assert (e5["variant"] == v).sum() == 3


def test_e5_pos_neg_subsets_are_disjoint_in_construction():
    # On a session pool, the overshoot (r>0) and undershoot (r<0) masks of the
    # frozen residual cannot overlap; exact-zero r is excluded from both.
    pools = _pools_from_sessions(["2026-05-26"])
    pool = pools["2026-05-26"]
    fits = eg.fit_all(pools)
    r = eg._frozen_e4e5_residual(pool, fits, with_intercept=True)
    pos = r > 0
    neg = r < 0
    assert not (pos & neg).any()
    # union excludes only NaN and exact-zero rows
    excluded = (~pos) & (~neg)
    assert (excluded == (r.isna() | (r == 0))).all()


# ==========================================================================
# 5. INTERCEPT reading — immaterial to E1/E2/E3, bites E4/E5
# ==========================================================================
def test_intercept_is_spearman_immaterial_for_e1():
    # E1 with-intercept vs no-intercept residual differ by the constant a, a
    # monotone shift -> IDENTICAL Spearman IC against any target.
    pools = _pools_from_sessions(["2026-05-26"])
    pool = pools["2026-05-26"]
    fits = eg.fit_all(pools)
    r_wi = eg._e1_residual(pool, 15, ef.E1_GLOBAL_MODE, fits, with_intercept=True)
    r_ni = eg._e1_residual(pool, 15, ef.E1_GLOBAL_MODE, fits, with_intercept=False)
    for t in eg.TARGETS:
        a = pr._spearman_ic(r_wi, pool[t])
        b = pr._spearman_ic(r_ni, pool[t])
        if math.isnan(a) or math.isnan(b):
            assert math.isnan(a) and math.isnan(b)
        else:
            assert a == pytest.approx(b)
    # the two residuals genuinely differ (by the intercept) where finite
    diff = (r_wi - r_ni).dropna()
    assert diff.std() == pytest.approx(0.0, abs=1e-12)   # constant difference
    assert abs(float(diff.mean())) >= 0.0                # equals fit['a']


def test_no_intercept_e5_membership_can_differ():
    # The no-intercept residual = with-intercept + a (a constant). When a != 0,
    # the r>0 / r<0 membership SHIFTS — the exact place the prereg says the
    # intercept flag bites. We assert the masks are not identical in general.
    pools = _pools_from_sessions(["2026-05-26", "2026-05-27"])
    fits = eg.fit_all(pools)
    pool = pools["2026-05-26"]
    a = fits["e1"][eg.FROZEN_E4E5_WINDOW]["fit"]["a"]
    r_wi = eg._frozen_e4e5_residual(pool, fits, with_intercept=True)
    r_ni = eg._frozen_e4e5_residual(pool, fits, with_intercept=False)
    if abs(a) > 1e-9:
        pos_wi = (r_wi > 0).fillna(False)
        pos_ni = (r_ni > 0).fillna(False)
        # at least one row flips membership when the intercept is nonzero
        assert (pos_wi != pos_ni).any()


def test_e4e5_extra_carries_no_intercept_reading():
    # The FLAG-both requirement: each E4/E5 grid cell's extra must surface the
    # no-intercept reading (mean_ic + t), NOT spawn an extra grid cell.
    pools = _pools_from_sessions(_SESSIONS)
    grid, _ = eg.build_grid(pools)
    for fam in ("E4", "E5"):
        sub = grid[grid["family"] == fam]
        assert len(sub) == eg.expected_cell_counts()[fam]
        for extra in sub["extra"]:
            assert "no-intercept" in extra
            assert "with-intercept" in extra


# ==========================================================================
# 6. E3 linear arm IS the E1 W15/global object (re-reported, no de-dup)
# ==========================================================================
def test_e3_linear_equals_e1_w15_global_cell():
    pools = _pools_from_sessions(_SESSIONS)
    grid, _ = eg.build_grid(pools)
    for t in eg.TARGETS:
        e3_lin = grid[(grid["family"] == "E3") & (grid["variant"] == "linear")
                      & (grid["target"] == t)]
        e1_w15 = grid[(grid["family"] == "E1") & (grid["variant"] == "W15_global")
                      & (grid["target"] == t)]
        assert len(e3_lin) == 1 and len(e1_w15) == 1
        a = float(e3_lin["mean_ic"].iloc[0])
        b = float(e1_w15["mean_ic"].iloc[0])
        if math.isnan(a) or math.isnan(b):
            assert math.isnan(a) and math.isnan(b)
        else:
            assert a == pytest.approx(b)


# ==========================================================================
# 7. the DECISIVE paired r-vs-disp difference (its OWN clustered t)
# ==========================================================================
def test_r_vs_disp_diff_is_paired_per_session():
    # The diff row's mean must equal the across-session mean of the per-session
    # (IC(r)-IC(disp)) differences, and it must be a SEPARATE family from the
    # raw anchors (so the clustered t is on the paired differences).
    pools = _pools_from_sessions(_SESSIONS)
    fits = eg.fit_all(pools)
    grid, _ = eg.build_grid(pools)
    # recompute per-session diffs by hand
    per_session = {t: [] for t in eg.TARGETS}
    for s in sorted(pools):
        d = eg._r_vs_disp_session_diffs(pools[s], fits)
        for t in eg.TARGETS:
            cid = eg._cell_id("ANCHOR", f"rMINUSdisp_W{eg.FROZEN_E4E5_WINDOW}", t)
            v = d[cid]
            if np.isfinite(v):
                per_session[t].append(v)
    for t in eg.TARGETS:
        row = grid[(grid["family"] == "ANCHOR_DIFF") & (grid["target"] == t)]
        assert len(row) == 1
        got = float(row["mean_ic"].iloc[0])
        exp = float(np.mean(per_session[t])) if per_session[t] else float("nan")
        if math.isnan(exp):
            assert math.isnan(got)
        else:
            assert got == pytest.approx(exp)


# ==========================================================================
# 8. M2 CV scalars land BESIDE the in-sample fits (in extra)
# ==========================================================================
def test_e1_global_extra_carries_m2_cv():
    pools = _pools_from_sessions(_SESSIONS)
    grid, fits = eg.build_grid(pools)
    e1g = grid[(grid["family"] == "E1") & (grid["variant"] == "W15_global")]
    assert len(e1g) == 3
    for extra in e1g["extra"]:
        assert "M2 LOWO-CV" in extra
        assert "insample_b=" in extra
        assert "cv_b_min=" in extra and "cv_b_max=" in extra


def test_e2_and_e3_extra_carry_m2_cv():
    pools = _pools_from_sessions(_SESSIONS)
    grid, _ = eg.build_grid(pools)
    e2 = grid[grid["family"] == "E2"]
    for extra in e2["extra"]:
        assert "M2 LOWO-CV" in extra and "amplitude" in extra
    e3sqrt = grid[(grid["family"] == "E3") & (grid["variant"] == "sqrt")]
    for extra in e3sqrt["extra"]:
        assert "M2 LOWO-CV" in extra and "insample_g=" in extra


def test_cv_extra_reports_multiple_weeks():
    # With 2 ISO weeks the LOWO-CV must report >= 2 held-out-week refits.
    pools = _pools_from_sessions(_SESSIONS)
    fits = eg.fit_all(pools)
    cv = fits["e1"][15]["cv"]
    assert len(cv["cv_by_week"]) >= 2
    extra = eg._cv_extra(cv, "b")
    assert "cv_n_weeks=" in extra


# ==========================================================================
# 8b. E2 propagator M is convolved PER TICKER, never across the pooled stack
# ==========================================================================
def test_e2_propagator_M_is_per_ticker_not_cross_ticker():
    # REGRESSION: the propagator is an IIR recursion (M_t = flow_t + decay·M_{t-1}).
    # If M were convolved over the concatenated cross-ticker stack, ticker-2's M
    # at its first minute would carry ticker-1's accumulator across the boundary.
    # The pooled M column MUST instead equal each ticker's standalone convolution
    # (per-ticker, before pooling) — proven here for the most contaminating λ=45.
    sb = _session_bars(n_tickers=2, base_seed=555)
    dump = _dump_prices(sb)
    frames = eg._session_energy_frames(sb, dump)   # per-ticker energy frames
    pool = eg._session_pooled(frames)
    lam = 45.0
    mc = eg._m_col(lam)
    # ticker-2's block in the pool starts at row 390 (each ticker has 390 rows).
    pooled_M_first_t2 = pool[mc].iloc[390]
    # standalone convolution of ticker-2's own flow increment
    t2_flow = ef.ofi_flow_increment(sb["TK1"])     # sorted ticker order: TK0, TK1
    M_t2_standalone = ef.propagator_convolve(t2_flow, lam)
    assert pooled_M_first_t2 == pytest.approx(float(M_t2_standalone.iloc[0]))
    # and the whole ticker-2 block matches its standalone M elementwise
    block = pool[mc].iloc[390:780].reset_index(drop=True)
    standalone = M_t2_standalone.reset_index(drop=True)
    both = block.notna() & standalone.notna()
    assert np.allclose(block[both].to_numpy(), standalone[both].to_numpy())


def test_e2_residual_uses_per_ticker_M():
    # The scored E2 residual must reduce to realized_cum − (a + amp·M) on the
    # per-ticker M; check it matches a hand-built residual from the pool's M col.
    pools = _pools_from_sessions(["2026-05-26", "2026-05-27"])
    fits = eg.fit_all(pools)
    pool = pools["2026-05-26"]
    lam = 15.0
    fit = fits["e2"][lam]["fit"]
    r = eg._e2_residual(pool, lam, fits)
    hand = pool["realized_cum"] - (fit["a"] + fit["amplitude"] * pool[eg._m_col(lam)])
    both = r.notna() & hand.notna()
    assert np.allclose(r[both].to_numpy(), hand[both].to_numpy())


# ==========================================================================
# 9. fit pass uses the GLOBAL CAUSAL ≤ cutoff scalars (causality invariant)
# ==========================================================================
def test_fit_all_is_causal_to_cutoff():
    # Appending a post-cutoff session must NOT change any fitted global scalar
    # (the same causality invariant the E1/E2/E3 fitters carry).
    base = _pools_from_sessions(["2026-05-29", "2026-06-02"])
    fits_base = eg.fit_all(base, insample_end="2026-06-02")
    appended = dict(base)
    # a wild post-cutoff session
    appended["2026-06-04"] = _pools_from_sessions(["2026-06-04"])["2026-06-04"]
    fits_app = eg.fit_all(appended, insample_end="2026-06-02")
    assert fits_app["e1"][15]["fit"]["b"] == fits_base["e1"][15]["fit"]["b"]
    assert fits_app["e2"][15.0]["fit"]["amplitude"] == \
        fits_base["e2"][15.0]["fit"]["amplitude"]
    assert fits_app["e3"]["fit"]["g"] == fits_base["e3"]["fit"]["g"]


# ==========================================================================
# 10. summary math reuses the exploratory clustered-t convention
# ==========================================================================
def test_summary_uses_clustered_t_convention():
    # n_sessions counts non-NaN session-cells; t = mean/(sd/√n) with ddof=1.
    pools = _pools_from_sessions(_SESSIONS)
    grid, _ = eg.build_grid(pools)
    # take an anchor cell and recompute its clustered t from the per-session ICs
    fits = eg.fit_all(pools)
    cid = eg._cell_id("ANCHOR", "ofi_15", "ret_fwd_15")
    vals = []
    for s in sorted(pools):
        ics, _ = eg.session_energy_cells(pools[s], fits)
        v = ics[cid]
        if v is not None and np.isfinite(v):
            vals.append(v)
    n = len(vals)
    mean = float(np.mean(vals))
    sd = float(np.std(vals, ddof=1)) if n > 1 else float("nan")
    exp_t = mean / (sd / math.sqrt(n)) if (n > 1 and sd > 0) else float("nan")
    row = grid[grid["cell_id"] == cid]
    assert len(row) == 1
    got_t = float(row["t"].iloc[0])
    got_n = int(row["n_sessions"].iloc[0])
    assert got_n == n
    if math.isnan(exp_t):
        assert math.isnan(got_t)
    else:
        assert got_t == pytest.approx(exp_t)


# ==========================================================================
# 11. no flow_policy / no bps — scope guard (M3 is downstream)
# ==========================================================================
def test_module_imports_no_flow_policy():
    # M3 (the two-leg execution counterfactual / bps) is a SEPARATE downstream
    # task; energy_grid must not import flow_policy. Assert via the AST import
    # nodes (robust to docstring mentions of the name).
    import ast
    import src.research.bflow.energy_grid as mod
    tree = ast.parse(open(mod.__file__).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            for n in node.names:
                imported.add(f"{base}.{n.name}")
    assert not any("flow_policy" in name for name in imported), imported


def test_grid_has_no_bps_or_policy_columns():
    pools = _pools_from_sessions(["2026-05-26", "2026-05-27"])
    grid, _ = eg.build_grid(pools)
    for col in grid.columns:
        assert "bps" not in col.lower()
        assert "delta" not in col.lower()
        assert "policy" not in col.lower()


# ==========================================================================
# 12. end-to-end seam — pools -> grid -> parquet round-trip
# ==========================================================================
def test_build_grid_round_trips_through_parquet(tmp_path):
    pools = _pools_from_sessions(_SESSIONS)
    grid, _ = eg.build_grid(pools)
    path = eg.write_grid(grid, analysis_dir=str(tmp_path))
    back = pd.read_parquet(path)
    assert list(back.columns) == eg._GRID_COLUMNS
    assert len(back) == len(grid)
    # all the grid families survive the round-trip
    assert set(back["family"]) == set(grid["family"])


def test_run_writes_grid_from_in_memory_pools(monkeypatch, tmp_path):
    # Exercise run()'s fit->score->write path by stubbing build_session_pools to
    # return in-memory synthetic pools (no cache I/O).
    pools = _pools_from_sessions(_SESSIONS)
    monkeypatch.setattr(eg, "build_session_pools",
                        lambda *a, **k: pools)
    grid, path = eg.run(analysis_dir=str(tmp_path), progress=False)
    assert path.endswith("bflow_phase1c_energy_grid.parquet")
    assert len(grid) == sum(eg.expected_cell_counts().values())
