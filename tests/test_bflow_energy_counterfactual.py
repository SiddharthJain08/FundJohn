"""Tests for src/research/bflow/energy_counterfactual.py (spec §2 method M3).

T3 of the Phase-1c ENERGY deep-dive. Binding spec
``analysis/bflow_phase1c_energy_grid_preenumeration.md`` §2 (M3 — the symmetric
two-leg execution counterfactual). M3 is the ONLY method that touches a synthetic
order book and bps; M1 (IC grid) / M2 (LOWO-CV) are the upstream T2 objects.

M3 in one paragraph (spec §2):
  For EVERY cached (ticker, session ≤ 2026-06-02) pair, simulate BOTH a BUY leg
  and a SELL leg on a SYNTHETIC book (not real intents), each timed by the FROZEN
  energy signal = E1 W=15 global-causal-β residual r. Trigger uses the
  SESSION-CENTERED r: subtract the RUNNING (trailing-only, causal) session mean,
  divide by the RUNNING session sd, then apply the z-threshold with the reversion
  sign — BUY enters when centered r ≤ −z (undershoot/upward pressure), SELL when
  centered r ≥ +z (overshoot). First crossing only; one entry per leg. Fill at
  vw_{t+1}; fallback = dump at minute 384 if never triggered; Phase-1 differential
  cost exactly as Test-B. Frozen z ∈ {0.5, 1.0, 1.5}, ALL reported. Scoring:
  per-leg per-session MEAN delta-bps (the session is the cluster), across-session
  clustered t per leg. PASS = BOTH legs INDIVIDUALLY clustered-positive; the
  buy+sell SUM is the drift-control DIAGNOSTIC, never the bar. Report trigger rate
  + fallback rate per z.

These tests LAYER the verification (per the advisor): the running-z function is
tested standalone (causality + warmup-NaN), the single-leg simulate on crafted
bars (trigger / vw_{t+1} fill / 384 fallback), and the aggregation / both-legs
criterion is driven by INJECTED per-pair delta_bps (so the drift-control test is
deterministic without engineering a both-legs-triggering price tape).
"""
import math

import numpy as np
import pandas as pd
import pytest

from src.research.bflow import energy_counterfactual as ec
from src.research.bflow import energy_features as ef
from src.research.bflow import energy_grid as eg
from src.research.bflow import flow_policy as fp


# ==========================================================================
# builders (mirror tests/test_bflow_flow_policy.py / test_bflow_energy_grid.py)
# ==========================================================================
def _row(minute, c, v=100.0, vw=None, h=None, l=None, o=None):
    """One bar row; defaults to a valid bar with h=l=c (zero spread), vw=c, o=c."""
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


def _with_dump(rows, dump_c=100.0, dump_v=100.0):
    """Append a 5-bar dump window (385..389) so dump_benchmark exists."""
    have = {r["minute"] for r in rows}
    out = list(rows)
    for m in range(385, 390):
        if m not in have:
            out.append(_row(m, c=dump_c, v=dump_v))
    return out


def _noisy_ticker(seed, with_dump=True):
    """Full 0..389 session for ONE ticker: noisy random-walk close so r / z have
    variance, valid dump window. Returns the bars DataFrame."""
    rng = np.random.default_rng(seed)
    closes = [100.0]
    for _ in range(389):
        closes.append(closes[-1] * (1.0 + rng.normal(scale=0.001)))
    vols = list(rng.uniform(80.0, 120.0, size=390))
    rows = [_row(m, c=closes[m], v=vols[m]) for m in range(390)]
    return make_df(rows)


# ==========================================================================
# 1. running causal z — standalone (causality + warmup NaN)
# ==========================================================================
def test_running_z_causal_future_minute_does_not_change_past():
    """The running z at minute t must NOT change when a LATER minute is altered
    (the strict-causality invariant; trailing-only running stats)."""
    rng = np.random.default_rng(7)
    r = pd.Series(rng.normal(size=60))
    z_full = ec.running_z(r)
    # Alter every minute strictly AFTER t0; z at t<=t0 must be byte-identical.
    t0 = 25
    r2 = r.copy()
    r2.iloc[t0 + 1:] = r2.iloc[t0 + 1:] + 999.0
    z2 = ec.running_z(r2)
    for t in range(0, t0 + 1):
        a, b = z_full.iloc[t], z2.iloc[t]
        assert (math.isnan(a) and math.isnan(b)) or a == b, (
            f"z at minute {t} changed when a later minute was altered")


def test_running_z_nan_before_two_finite_obs():
    """z is NaN until there are >= 2 finite trailing observations (sd undefined
    on < 2). With t-inclusive running stats the first finite-r minute has n=1 ->
    NaN; the second finite minute is the earliest possibly-finite z."""
    r = pd.Series([np.nan, np.nan, 1.0, 2.0, 3.0, 4.0])
    z = ec.running_z(r)
    assert math.isnan(z.iloc[0])
    assert math.isnan(z.iloc[1])
    assert math.isnan(z.iloc[2])           # only 1 finite obs so far -> NaN
    assert np.isfinite(z.iloc[3])          # 2 finite obs -> defined


def test_running_z_t_inclusive_value():
    """Hand-computed: for r=[1,3] the running z at t=1 uses BOTH points
    (t-inclusive): mean=2, sd(ddof=1)=√2, z_1 = (3-2)/√2."""
    r = pd.Series([1.0, 3.0])
    z = ec.running_z(r)
    assert math.isnan(z.iloc[0])
    assert z.iloc[1] == pytest.approx((3.0 - 2.0) / math.sqrt(2.0))


def test_running_z_zero_variance_is_nan():
    """A constant trailing window has sd=0 -> z NaN (no crossing can fire)."""
    r = pd.Series([5.0, 5.0, 5.0, 5.0])
    z = ec.running_z(r)
    assert z.iloc[0:].isna().all() or (z.dropna() == 0.0).all() is False
    # explicit: every defined-window value is NaN because sd==0.
    assert z.isna().all()


# ==========================================================================
# 2. frozen-signal β identity with the grid (causal, not lookahead)
# ==========================================================================
def test_frozen_fit_is_grid_global_causal_beta():
    """The M3 frozen signal MUST be byte-identical to energy_grid's E1 W=15
    GLOBAL-CAUSAL β (an execution sim must be causal — never the per-session
    lookahead). We reuse the grid's own fit path so they cannot diverge."""
    pools = {
        "2026-05-01": eg._session_pooled(
            eg._session_energy_frames(
                {"AAA": _noisy_ticker(1)}, {"AAA": 100.0})),
        "2026-05-02": eg._session_pooled(
            eg._session_energy_frames(
                {"BBB": _noisy_ticker(2)}, {"BBB": 100.0})),
    }
    frozen = ec.frozen_e1_fit(pools)
    grid_fit = eg.fit_all(pools)["e1"][ec.FROZEN_WINDOW]["fit"]
    assert frozen["a"] == grid_fit["a"]
    assert frozen["b"] == grid_fit["b"]
    assert frozen["mode"] == ef.E1_GLOBAL_MODE


def test_signal_residual_per_ticker_matches_apply_linear():
    """The per-ticker M3 residual r is exactly ef.apply_linear_residual on this
    ticker's own disp_15 / ofi_15 with the frozen (a,b) — never pooled."""
    tdf = _noisy_ticker(3)
    fit = {"a": 0.0001, "b": 0.5, "mode": ef.E1_GLOBAL_MODE}
    feats = ef.compute_energy_features(tdf)
    expected = ef.apply_linear_residual(feats["disp_15"], feats["ofi_15"], fit)
    got = ec.ticker_signal(tdf, fit)
    pd.testing.assert_series_equal(
        got.reset_index(drop=True), expected.reset_index(drop=True),
        check_names=False)


# ==========================================================================
# 3. single-leg simulate on crafted bars (trigger / vw_{t+1} / fallback)
# ==========================================================================
def _const_r_with_one_dip(dip_minute):
    """Build an r-series (already the energy signal) whose running-z stays
    ~0 (sub-threshold) until a single strong spike at ``dip_minute`` — so a leg
    crosses exactly once, at dip_minute.

    Construction: r is 0 everywhere EXCEPT two tiny seed values at minutes 0/1
    (so the running sd is finite and > 0 from minute 1 on; a perfectly constant
    prefix would give sd=0 -> z NaN -> nothing ever crosses). Every plain-0 minute
    then has z = (0 − ~0)/(tiny sd) ≈ 0, which is below ±z for any z in the grid.
    The spike at ``dip_minute`` is a large outlier vs the ~0-mean / tiny-sd prefix
    so its z is huge — the FIRST (and only) crossing. The caller sets the spike
    sign (−1.0 for a BUY undershoot, +1.0 for a SELL overshoot)."""
    r = pd.Series(0.0, index=range(390))
    r.iloc[0] = 1e-9
    r.iloc[1] = -1e-9
    r.iloc[dip_minute] = -1.0          # default: a big undershoot (BUY leg)
    return r


def test_single_leg_buy_triggers_and_fills_vw_tplus1():
    """BUY leg: centered r crosses <= -z at the dip minute t; fill = vw_{t+1};
    delta_bps via fp._delta_bps (LONG)."""
    dip = 100
    r = _const_r_with_one_dip(dip)
    # bars: vw distinct per minute so we can see WHICH minute filled.
    rows = [_row(m, c=100.0, vw=100.0 + 0.01 * m) for m in range(385)]
    rows = _with_dump(rows, dump_c=100.0)
    bars = make_df(rows)
    p_dump = 100.0
    res = ec.simulate_leg(bars, r, leg="buy", z=1.0, p_eod_dump=p_dump)
    assert res["triggered"] is True
    assert res["used_fallback"] is False
    # decision at dip minute t, fill at t+1.
    assert res["entry_minute"] == dip + 1
    assert res["entry_price"] == pytest.approx(100.0 + 0.01 * (dip + 1))


def test_single_leg_sell_triggers_on_positive_spike():
    """SELL leg mirrors: centered r crosses >= +z on a strong POSITIVE spike."""
    spike = 120
    r = _const_r_with_one_dip(spike)
    r.iloc[spike] = +1.0               # overshoot
    rows = [_row(m, c=100.0, vw=100.0 + 0.01 * m) for m in range(385)]
    rows = _with_dump(rows, dump_c=100.0)
    bars = make_df(rows)
    res = ec.simulate_leg(bars, r, leg="sell", z=1.0, p_eod_dump=100.0)
    assert res["triggered"] is True
    assert res["entry_minute"] == spike + 1


def test_single_leg_no_cross_falls_back_at_dump():
    """A flat r-tape with no crossing -> forced fallback at minute 384: entered
    AT the dump, used_fallback=True, delta_bps == 0.0 (mirrors flow_policy)."""
    r = pd.Series(0.0, index=range(390))   # exactly constant -> z all NaN
    rows = _with_dump([_row(m, c=100.0) for m in range(385)], dump_c=100.0)
    bars = make_df(rows)
    res = ec.simulate_leg(bars, r, leg="buy", z=1.0, p_eod_dump=100.0)
    assert res["triggered"] is False
    assert res["used_fallback"] is True
    assert res["delta_bps"] == pytest.approx(0.0)


def test_single_leg_scan_starts_at_30():
    """A crossing BEFORE SCAN_START_MINUTE (30) is ignored; only crossings at
    minute >= 30 can fire (flow_policy convention reused)."""
    r = _const_r_with_one_dip(10)      # dip at minute 10 (< 30)
    rows = _with_dump([_row(m, c=100.0, vw=100.0 + 0.01 * m) for m in range(385)])
    bars = make_df(rows)
    res = ec.simulate_leg(bars, r, leg="buy", z=1.0, p_eod_dump=100.0)
    # the only crossing is pre-scan -> no entry -> fallback.
    assert res["triggered"] is False
    assert res["used_fallback"] is True


def test_single_leg_lapse_on_invalid_fill_continues():
    """If the t+1 fill bar is invalid the trigger LAPSES and scanning continues;
    a later valid crossing still fills. Reuses the flow_policy lapse semantics."""
    r = pd.Series(0.0, index=range(390))
    r.iloc[0] = 1e-9                    # seed a finite, > 0 running sd
    r.iloc[1] = -1e-9
    r.iloc[50] = -1.0                  # first dip at 50 (fill bar 51 invalid)
    r.iloc[200] = -1.0                 # later dip at 200 (fill bar 201 valid)
    rows = []
    for m in range(385):
        if m == 51:
            rows.append(_row(m, c=100.0, vw=0.0, v=0.0, h=0.0, l=0.0))  # invalid
        else:
            rows.append(_row(m, c=100.0, vw=100.0 + 0.01 * m))
    rows = _with_dump(rows, dump_c=100.0)
    bars = make_df(rows)
    res = ec.simulate_leg(bars, r, leg="buy", z=1.0, p_eod_dump=100.0)
    assert res["triggered"] is True
    assert res["entry_minute"] == 201   # lapsed at 51, filled at 200+1


# ==========================================================================
# 4. aggregation / both-legs criterion — driven by INJECTED per-pair delta_bps
# ==========================================================================
def _leg_result(delta_bps, triggered=True, used_fallback=False):
    return {"triggered": triggered, "entry_minute": 100,
            "entry_price": 100.0, "used_fallback": used_fallback,
            "delta_bps": float(delta_bps)}


def test_both_legs_criterion_fails_under_pure_drift():
    """DRIFT CASE: a symmetric session-level drift helps the BUY leg by +d and
    hurts the SELL leg by −d EVERY session. Per session buy = +d > 0,
    sell = −d < 0 (with variance across sessions so t is DEFINED). The both-legs
    criterion MUST fail because the sell leg is clustered-NEGATIVE — drift can
    never make BOTH legs positive. The buy+sell SUM is ~0 (the drift cancels in
    the sum), which is exactly why the sum is the drift-control DIAGNOSTIC and not
    the bar: a strongly-positive single leg would otherwise look like alpha."""
    drifts = [+10.0, +12.0, +8.0, +11.0, +9.0]   # varying so sd > 0 -> t defined
    per_session = {}
    for i, d in enumerate(drifts):
        s = f"2026-05-0{i+1}"
        per_session[s] = {
            "buy": [_leg_result(+d)],
            "sell": [_leg_result(-d)],
        }
    summary = ec.summarize_counterfactual(per_session, z=1.0)
    buy = summary[(summary["leg"] == "buy")].iloc[0]
    sell = summary[(summary["leg"] == "sell")].iloc[0]
    assert buy["t"] > 0                       # buy leg looks great in isolation
    assert sell["t"] < 0                       # sell leg clustered-NEGATIVE
    # both-legs pass criterion: BOTH t > 0. Sell t < 0 -> FAILS (the drift fix).
    assert ec.both_legs_positive(summary) is False
    # the SUM diagnostic cancels to ~0 (symmetric drift) and is NOT the bar.
    diag = summary[summary["leg"] == "sum_diagnostic"].iloc[0]
    assert diag["mean_bps"] == pytest.approx(0.0)


def test_both_legs_criterion_passes_under_symmetric_reversion():
    """SYMMETRIC REVERSION CASE: both legs individually positive across sessions
    -> the pass criterion is met."""
    # Per-session means vary (so the sample sd > 0 -> a defined t) but every
    # session is strictly positive on BOTH legs -> both clustered-positive.
    buy_vals = [+8.0, +7.0, +9.0, +6.5, +8.5, +7.5]
    sell_vals = [+6.0, +5.0, +7.0, +6.5, +5.5, +6.5]
    per_session = {}
    for i in range(6):
        s = f"2026-05-0{i+1}"
        per_session[s] = {
            "buy": [_leg_result(buy_vals[i])],
            "sell": [_leg_result(sell_vals[i])],
        }
    summary = ec.summarize_counterfactual(per_session, z=1.0)
    assert ec.both_legs_positive(summary) is True
    buy = summary[summary["leg"] == "buy"].iloc[0]
    sell = summary[summary["leg"] == "sell"].iloc[0]
    assert buy["t"] > 0 and sell["t"] > 0


def test_session_is_the_cluster_not_the_ticker():
    """n_sessions must count SESSIONS, not tickers. A session with 4 tickers
    collapses to ONE per-session mean -> n_sessions counts the session once."""
    per_session = {
        "2026-05-01": {"buy": [_leg_result(2.0), _leg_result(4.0),
                               _leg_result(6.0), _leg_result(8.0)],
                       "sell": [_leg_result(1.0), _leg_result(1.0),
                                _leg_result(1.0), _leg_result(1.0)]},
        "2026-05-02": {"buy": [_leg_result(5.0)],
                       "sell": [_leg_result(3.0)]},
    }
    summary = ec.summarize_counterfactual(per_session, z=1.0)
    buy = summary[summary["leg"] == "buy"].iloc[0]
    # 2 sessions -> n_sessions == 2 (NOT 5 tickers).
    assert buy["n_sessions"] == 2
    # session-1 buy mean = (2+4+6+8)/4 = 5; session-2 = 5 -> overall mean 5.
    assert buy["mean_bps"] == pytest.approx(5.0)


def test_trigger_and_fallback_rates_reported():
    """Per z, the trigger rate (fraction of pairs that triggered) and fallback
    rate (fraction that fell back) are reported per leg."""
    per_session = {
        "2026-05-01": {
            "buy": [_leg_result(5.0, triggered=True, used_fallback=False),
                    _leg_result(0.0, triggered=False, used_fallback=True)],
            "sell": [_leg_result(3.0, triggered=True, used_fallback=False),
                     _leg_result(3.0, triggered=True, used_fallback=False)],
        },
    }
    summary = ec.summarize_counterfactual(per_session, z=1.0)
    buy = summary[summary["leg"] == "buy"].iloc[0]
    sell = summary[summary["leg"] == "sell"].iloc[0]
    assert buy["trigger_rate"] == pytest.approx(0.5)
    assert buy["fallback_rate"] == pytest.approx(0.5)
    assert sell["trigger_rate"] == pytest.approx(1.0)
    assert sell["fallback_rate"] == pytest.approx(0.0)


def test_summary_carries_z_and_clustered_t_convention():
    """mean_bps / t use the clustered convention t = mean/(sd/√n), ddof=1, n<2 ->
    NaN t. Two sessions buy=[+10,+20] -> mean 15, sd √50, t = 15/(√50/√2)."""
    per_session = {
        "2026-05-01": {"buy": [_leg_result(10.0)], "sell": [_leg_result(0.0)]},
        "2026-05-02": {"buy": [_leg_result(20.0)], "sell": [_leg_result(0.0)]},
    }
    summary = ec.summarize_counterfactual(per_session, z=0.5)
    buy = summary[summary["leg"] == "buy"].iloc[0]
    assert buy["z"] == pytest.approx(0.5)
    assert buy["mean_bps"] == pytest.approx(15.0)
    sd = math.sqrt(((10 - 15) ** 2 + (20 - 15) ** 2) / 1)   # ddof=1 -> /1
    expected_t = 15.0 / (sd / math.sqrt(2))
    assert buy["t"] == pytest.approx(expected_t)


def test_single_session_t_is_nan():
    """With one session sd is undefined (ddof=1, n<2) -> t NaN (no spurious
    significance from a single cluster)."""
    per_session = {
        "2026-05-01": {"buy": [_leg_result(10.0)], "sell": [_leg_result(5.0)]},
    }
    summary = ec.summarize_counterfactual(per_session, z=1.0)
    buy = summary[summary["leg"] == "buy"].iloc[0]
    assert math.isnan(buy["t"])
    assert ec.both_legs_positive(summary) is False   # cannot pass on NaN t


# ==========================================================================
# 5. intercept invariance for M3 (z is shift-invariant)
# ==========================================================================
def test_running_z_shift_invariant():
    """Adding a constant (the OLS intercept a) to r shifts the running mean by a
    and leaves the running sd untouched -> (r-μ)/σ is unchanged. So M3 needs only
    the canonical with-intercept residual; there is no no-intercept axis."""
    rng = np.random.default_rng(11)
    r = pd.Series(rng.normal(size=80))
    z = ec.running_z(r)
    z_shift = ec.running_z(r + 3.14159)
    pd.testing.assert_series_equal(z, z_shift, check_names=False)


# ==========================================================================
# 6. end-to-end shape / determinism (small synthetic 2-session universe)
# ==========================================================================
def _build_pools_2sessions():
    return {
        "2026-05-01": eg._session_pooled(eg._session_energy_frames(
            {"AAA": _noisy_ticker(101), "BBB": _noisy_ticker(102)},
            {"AAA": 100.0, "BBB": 100.0})),
        "2026-05-02": eg._session_pooled(eg._session_energy_frames(
            {"CCC": _noisy_ticker(103)}, {"CCC": 100.0})),
    }


def _session_bars_2():
    return {
        "2026-05-01": {"AAA": _noisy_ticker(101), "BBB": _noisy_ticker(102)},
        "2026-05-02": {"CCC": _noisy_ticker(103)},
    }


def test_build_counterfactual_shape_and_z_grid():
    """The full counterfactual frame has 6 leg-rows (3 z × {buy,sell}) PLUS the
    3 sum-diagnostic rows (one per z); every required column present; z grid is
    exactly {0.5, 1.0, 1.5}."""
    pools = _build_pools_2sessions()
    session_bars = _session_bars_2()
    dump = {s: {tk: 100.0 for tk in session_bars[s]} for s in session_bars}
    frame = ec.build_counterfactual(pools, session_bars, dump)
    for col in ec.OUTPUT_COLUMNS:
        assert col in frame.columns
    zs = sorted(frame["z"].unique())
    assert zs == [0.5, 1.0, 1.5]
    legs = set(frame["leg"].unique())
    assert {"buy", "sell", "sum_diagnostic"}.issubset(legs)
    # 3 z × (buy + sell + sum_diagnostic) = 9 rows.
    assert len(frame) == 9


def test_build_counterfactual_deterministic():
    """Same inputs -> identical frame (research reproducibility)."""
    pools = _build_pools_2sessions()
    session_bars = _session_bars_2()
    dump = {s: {tk: 100.0 for tk in session_bars[s]} for s in session_bars}
    f1 = ec.build_counterfactual(pools, session_bars, dump)
    f2 = ec.build_counterfactual(pools, session_bars, dump)
    pd.testing.assert_frame_equal(f1, f2)


def test_fill_cost_uses_flow_policy_delta_bps():
    """The leg delta_bps is computed by fp._delta_bps verbatim (cost parity with
    Test-B). We craft a single-cross BUY where the entry sits BELOW the dump so
    the LONG gross is positive, and check it equals the fp computation."""
    dip = 100
    r = _const_r_with_one_dip(dip)
    # entry vw at t+1 = 99.0 (below dump 100) -> LONG gross > 0.
    rows = [_row(m, c=100.0, vw=99.0) for m in range(385)]
    rows = _with_dump(rows, dump_c=100.0)
    bars = make_df(rows)
    res = ec.simulate_leg(bars, r, leg="buy", z=1.0, p_eod_dump=100.0)
    entry_price = res["entry_price"]
    fill_min = res["entry_minute"]
    work, _valid = __import__(
        "src.research.bflow.flow_features", fromlist=["x"])._reindex_valid_frame(bars)
    import src.research.bflow.oracle as oracle
    entry_spread = oracle.spread_bps({
        "vw": entry_price,
        "h": float(work["h"].loc[fill_min]),
        "l": float(work["l"].loc[fill_min]),
    })
    expected = fp._delta_bps(100.0, entry_price, entry_spread, bars, "LONG")
    assert res["delta_bps"] == pytest.approx(expected)


def test_writer_roundtrip(tmp_path):
    """The parquet writer round-trips the tidy frame."""
    pools = _build_pools_2sessions()
    session_bars = _session_bars_2()
    dump = {s: {tk: 100.0 for tk in session_bars[s]} for s in session_bars}
    frame = ec.build_counterfactual(pools, session_bars, dump)
    path = ec.write_counterfactual(frame, analysis_dir=str(tmp_path))
    back = pd.read_parquet(path)
    assert len(back) == len(frame)
    assert list(back.columns) == list(frame.columns)
