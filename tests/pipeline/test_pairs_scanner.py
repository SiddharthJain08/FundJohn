"""Tests for src/pipeline/pairs_scanner.py (Task D1: pairs scanner + ledger).

Synthetic-only: no real DB, no real prices.parquet. The DB-fetching and
parquet-reading module functions (`_fetch_active_universe`,
`_load_bucket_closes`, `_load_cost_bps`) are monkey-patched so `run_scan`
runs entirely against fabricated data written to tmp_path. Tickers use the
ZZT- prefix per the brief (never a real-looking symbol like "AAA", which is
a real ETF).
"""
from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np
import pandas as pd
import psycopg2
import pytest

from src.pipeline import pairs_scanner as ps


# ── 1. BH-FDR ────────────────────────────────────────────────────────────────
def test_bh_fdr_hand_computed():
    pvals = [0.001, 0.01, 0.02, 0.04, 0.9]
    q = ps.bh_fdr(pvals)
    expected = [0.005, 0.025, 0.02 * 5 / 3, 0.05, 0.9]
    for got, exp in zip(q, expected):
        assert got == pytest.approx(exp, rel=1e-9)
    passes = [x < 0.10 for x in q]
    assert passes == [True, True, True, True, False]


def test_bh_fdr_empty():
    assert ps.bh_fdr([]) == []


# ── 2. Half-life ─────────────────────────────────────────────────────────────
def test_half_life_ar1_within_15pct():
    rng = np.random.default_rng(42)
    n = 2000
    phi = 0.9
    sigma_e = 1.0
    spread = np.zeros(n)
    for t in range(1, n):
        spread[t] = phi * spread[t - 1] + rng.normal(0, sigma_e)
    hl = ps.ar1_half_life(spread)
    true_hl = math.log(2) / -math.log(phi)  # ~6.58d
    assert hl is not None
    assert hl == pytest.approx(true_hl, rel=0.15)


def test_half_life_hard_fails_when_non_mean_reverting():
    """An explosive AR(1) (phi=1.05 > 1) has a POSITIVE coef on spread_{t-1}
    in the delta-regression (theta = -coef < 0) -> ar1_half_life must hard-fail
    (return None) rather than emit a nonsensical negative/inverted half-life."""
    rng = np.random.default_rng(7)
    n = 500
    explosive = np.zeros(n)
    for t in range(1, n):
        explosive[t] = 1.05 * explosive[t - 1] + rng.normal(0, 0.01)
    assert ps.ar1_half_life(explosive) is None


# ── 3. Cost gate ─────────────────────────────────────────────────────────────
def test_cost_gate_passes():
    assert ps.cost_ok(sigma_spread=0.02, cost_a_bps=10.0, cost_b_bps=10.0, cost_k=2.0) is True


def test_cost_gate_fails():
    assert ps.cost_ok(sigma_spread=0.004, cost_a_bps=10.0, cost_b_bps=10.0, cost_k=2.0) is False


# ── NaN-close coverage regression ───────────────────────────────────────────
def test_build_aligned_window_excludes_nan_closes_from_n_obs():
    """A masked/missing close (NaN) must not count toward n_obs or satisfy
    the min_obs_frac*window coverage floor -- math.log(nan) silently returns
    nan rather than raising, so the filter has to happen before the window
    tail-slice/intersection, not rely on a log()-time exception."""
    as_of = dt.date(2026, 8, 10)
    dates = [dt.date(2026, 8, 10) - dt.timedelta(days=i) for i in range(10)][::-1]
    date_strs = [d.isoformat() for d in dates]
    closes_a = [100.0 + i for i in range(10)]
    closes_a[3] = float("nan")  # one bad observation
    closes_b = [50.0 + i for i in range(10)]
    dated_a = list(zip(date_strs, closes_a))
    dated_b = list(zip(date_strs, closes_b))

    # window=10, min_obs_frac=0.9 -> need >=9 usable overlapping obs; with one
    # NaN dropped only 9 remain, so a naive (non-filtering) implementation
    # would have reported n_obs=10 (silently including the NaN row).
    built = ps.build_aligned_window(dated_a, dated_b, as_of, window=10, min_obs_frac=0.9)
    assert built is not None
    _, log_a, log_b, n_obs = built
    assert n_obs == 9
    assert not np.isnan(log_a).any()

    # Drop the coverage floor by one more bad obs -> now below 0.9*10=9 -> None.
    closes_a2 = list(closes_a)
    closes_a2[7] = float("nan")
    built2 = ps.build_aligned_window(list(zip(date_strs, closes_a2)), dated_b, as_of,
                                      window=10, min_obs_frac=0.9)
    assert built2 is None


# ── 5. Bucket cap ────────────────────────────────────────────────────────────
def test_bucket_cap_at_50():
    rows = [
        {"ticker": f"ZZT{i:03d}", "industry": "Widgets", "sector": "Industrials",
         "market_cap": float(1000 - i)}
        for i in range(60)
    ]
    buckets = ps.build_buckets(rows, cap=50)
    assert "Widgets" in buckets
    assert len(buckets["Widgets"]) == 50
    # highest market_cap entries preferred (market_cap = 1000-i, i=0..49 kept)
    kept = {r["ticker"] for r in buckets["Widgets"]}
    assert "ZZT000" in kept
    assert "ZZT059" not in kept


def test_bucket_drops_size_one_and_both_null():
    rows = [
        {"ticker": "ZZTLONE", "industry": "Solo", "sector": None, "market_cap": 1.0},
        {"ticker": "ZZTNUL1", "industry": None, "sector": None, "market_cap": 1.0},
        {"ticker": "ZZTNUL2", "industry": None, "sector": None, "market_cap": 1.0},
        {"ticker": "ZZTFBK1", "industry": None, "sector": "Fallback", "market_cap": 1.0},
        {"ticker": "ZZTFBK2", "industry": None, "sector": "Fallback", "market_cap": 1.0},
    ]
    buckets = ps.build_buckets(rows, cap=50)
    assert "Solo" not in buckets  # bucket size 1 dropped
    assert None not in buckets    # both-null rows dropped entirely
    assert "Fallback" in buckets and len(buckets["Fallback"]) == 2


# ── Synthetic price-panel helpers for the integration-style tests ──────────
def _make_cointegrated_pair(rng, n, beta=1.4, phi_noise=0.9, sigma_eps=0.01, start=100.0):
    """B = random walk in log space; A_log = beta*B_log + a stationary AR(1)
    noise process (phi_noise<1 so the noise -- and hence the spread -- is
    mean-reverting with a half-life inside [5,30]d; iid-per-level noise would
    still make the LEVELS cointegrated but tanks the daily-RETURN correlation
    used by the Pearson prefilter, since consecutive iid draws add large
    return-on-return variance unrelated to B's moves)."""
    b_log = np.cumsum(rng.normal(0, 0.01, n)) + math.log(start)
    noise = np.zeros(n)
    for t in range(1, n):
        noise[t] = phi_noise * noise[t - 1] + rng.normal(0, sigma_eps)
    a_log = beta * b_log + noise
    return np.exp(a_log), np.exp(b_log)


def _make_independent_walk(rng, n, start=100.0):
    log_p = np.cumsum(rng.normal(0, 0.01, n)) + math.log(start)
    return np.exp(log_p)


def _make_correlated_not_cointegrated(ref_log, seed=1, sigma=0.01):
    """A negative control for item 8: return-CORRELATED with `ref_log` (a
    reference log-price path) but NOT cointegrated with it -- the classic
    spurious-correlation-vs-cointegration distinction. Built as
    `ref_log + an INDEPENDENT random walk`: since the added component is
    itself a random walk (integrated of order 1, non-mean-reverting), no
    linear combination of the result and `ref_log` is stationary, so
    Engle-Granger must not find cointegration; but because `ref_log`'s own
    variance still dominates the sum, the two series' daily log-RETURNS
    stay correlated well above the 0.6 Pearson prefilter (empirically ~0.6-
    0.77 here). Uses its own FIXED, independent seed (decoupled from the
    panel's own `rng`) chosen (verified against this fixture's ZZTB path at
    seed=99) so the resulting eg_pvalue clears 0.10 by a wide, non-flaky
    margin regardless of BH-FDR's pool composition."""
    rng = np.random.default_rng(seed)
    n = len(ref_log)
    walk_log = np.cumsum(rng.normal(0, sigma, n))
    return np.exp(np.asarray(ref_log) + walk_log)


def _dated(closes, dates):
    return list(zip([d.isoformat() for d in dates], [float(c) for c in closes]))


def _bdates_ending(as_of, n):
    end = pd.Timestamp(as_of)
    idx = pd.bdate_range(end=end, periods=n)
    return [d.date() for d in idx]


def _build_panel(as_of, n, seed):
    rng = np.random.default_rng(seed)
    dates = _bdates_ending(as_of, n)
    a, b = _make_cointegrated_pair(rng, n)
    c = _make_independent_walk(rng, n)
    d = _make_independent_walk(rng, n)
    e = _make_correlated_not_cointegrated(np.log(b))
    return {
        "ZZTA": _dated(a, dates),
        "ZZTB": _dated(b, dates),
        "ZZTC": _dated(c, dates),
        "ZZTD": _dated(d, dates),
        "ZZTE": _dated(e, dates),
    }


def _install_fakes(monkeypatch, panel_by_as_of, cost_bps=None):
    def fake_universe(uri=None, table=None):
        return [
            {"ticker": "ZZTA", "industry": "ZZ-Widgets", "sector": "ZZ-Industrials", "market_cap": 5.0},
            {"ticker": "ZZTB", "industry": "ZZ-Widgets", "sector": "ZZ-Industrials", "market_cap": 4.0},
            {"ticker": "ZZTC", "industry": "ZZ-Widgets", "sector": "ZZ-Industrials", "market_cap": 3.0},
            {"ticker": "ZZTD", "industry": "ZZ-Widgets", "sector": "ZZ-Industrials", "market_cap": 2.0},
            {"ticker": "ZZTE", "industry": "ZZ-Widgets", "sector": "ZZ-Industrials", "market_cap": 1.0},
        ]

    def fake_closes(tickers, as_of, window):
        panel = panel_by_as_of(as_of)
        return {t: panel[t] for t in tickers if t in panel}

    def fake_cost_bps():
        return cost_bps or {}

    monkeypatch.setattr(ps, "_fetch_active_universe", fake_universe)
    monkeypatch.setattr(ps, "_load_bucket_closes", fake_closes)
    monkeypatch.setattr(ps, "_load_cost_bps", fake_cost_bps)


# ── 4. Persistence rule ──────────────────────────────────────────────────────
def test_persistence_rule_two_consecutive_passes(tmp_path, monkeypatch):
    out_path = tmp_path / "pair_ledger.parquet"
    as_of_1 = dt.date(2026, 8, 10)   # Monday
    as_of_2 = dt.date(2026, 8, 17)   # following Monday
    n = 150

    def panel_for(as_of):
        # Build one long fixed panel ending at as_of_2, and reuse the same
        # underlying series for as_of_1's earlier window so the pair's
        # cointegration signal is stable across both scans.
        return _build_panel(as_of_2, n + 10, seed=99)

    _install_fakes(monkeypatch, panel_for, cost_bps={"ZZTA": 1.0, "ZZTB": 1.0, "ZZTC": 1.0, "ZZTD": 1.0, "ZZTE": 1.0})

    window = 100
    summary1 = ps.run_scan(as_of=as_of_1, window=window, min_corr=0.6,
                            fdr_q_threshold=0.10, cost_k=2.0, out_path=str(out_path),
                            corr_lookback_days=90, min_obs_frac=0.9)
    assert summary1["approved"] == 0, "first-ever appearance must always be approved=False"

    df1 = pd.read_parquet(out_path)
    assert set(df1["approved"]) == {False}

    summary2 = ps.run_scan(as_of=as_of_2, window=window, min_corr=0.6,
                            fdr_q_threshold=0.10, cost_k=2.0, out_path=str(out_path),
                            corr_lookback_days=90, min_obs_frac=0.9)

    df2 = pd.read_parquet(out_path)
    df2_latest = df2[df2["as_of"] == as_of_2]
    # The genuinely cointegrated ZZTA/ZZTB pair should pass fdr both times ->
    # approved True on the second scan; the independent-walk pairs never pass.
    ab_row = df2_latest[
        (df2_latest["ticker_a"].isin(["ZZTA", "ZZTB"])) & (df2_latest["ticker_b"].isin(["ZZTA", "ZZTB"]))
    ]
    assert len(ab_row) == 1
    assert bool(ab_row.iloc[0]["fdr_pass"]) is True
    assert bool(ab_row.iloc[0]["approved"]) is True

    # replace-on-rescan only replaces rows for the SAME as_of being written;
    # as_of_1's rows must still be present untouched after scanning as_of_2.
    assert (df2["as_of"] == as_of_1).sum() == len(df1)


def test_full_scan_schema_and_summary(tmp_path, monkeypatch):
    out_path = tmp_path / "pair_ledger.parquet"
    as_of = dt.date(2026, 8, 10)
    n = 150

    def panel_for(_as_of):
        return _build_panel(as_of, n, seed=99)

    _install_fakes(monkeypatch, panel_for, cost_bps={"ZZTA": 1.0, "ZZTB": 1.0, "ZZTC": 1.0, "ZZTD": 1.0, "ZZTE": 1.0})

    summary = ps.run_scan(as_of=as_of, window=100, min_corr=0.6, fdr_q_threshold=0.10,
                           cost_k=2.0, out_path=str(out_path), corr_lookback_days=90,
                           min_obs_frac=0.9)
    assert summary["buckets"] == 1
    assert summary["pairs_tested"] >= 1
    assert summary["approved"] == 0  # first-ever appearance

    df = pd.read_parquet(out_path)
    expected_cols = ["as_of", "ticker_a", "ticker_b", "industry", "beta", "alpha",
                      "half_life_days", "sigma_spread", "spread_mean", "eg_pvalue",
                      "fdr_q", "fdr_pass", "cost_ok", "approved", "n_obs"]
    assert list(df.columns) == expected_cols

    # item 8: the OLD version of this assertion (checking ZZTC/ZZTD, plain
    # independent walks) passed VACUOUSLY -- those pairs' return correlation
    # with anything else never clears the 0.6 Pearson prefilter with this
    # fixture's seed, so walk_rows was always empty and `not empty.any()` is
    # trivially True regardless of whether the fdr/coint logic works at all.
    # ZZTE (see _make_correlated_not_cointegrated) is a REAL negative
    # control: engineered to be return-correlated with ZZTB well above 0.6
    # (so it reliably clears the prefilter and gets a real EG cointegration
    # test run on it) while NOT actually being cointegrated with it (so it
    # must fail fdr_pass). Assert it was actually tested, not just absent.
    walk_rows = df[(df["ticker_a"] == "ZZTE") | (df["ticker_b"] == "ZZTE")]
    assert len(walk_rows) >= 1, (
        "ZZTE was designed to clear the Pearson prefilter and be a real "
        "coint() test case -- if it's absent, the fixture/prefilter broke"
    )
    assert not walk_rows["fdr_pass"].any()


# ── item 1: NaN eg_pvalue must never enter the BH-FDR pool ─────────────────
def test_nan_eg_pvalue_dropped_before_bh_fdr(tmp_path, monkeypatch):
    """A pair whose evaluate_pair() produces a non-finite eg_pvalue (e.g. a
    coint() degenerate return) must be filtered out BEFORE bh_fdr runs --
    never fed into the pool, which would otherwise silently inflate `n` and
    every other pair's fdr_q -- and counted in errors_dropped instead."""
    out_path = tmp_path / "pair_ledger.parquet"
    as_of = dt.date(2026, 8, 10)

    def fake_universe(uri=None, table=None):
        return [
            {"ticker": "ZZTP", "industry": "ZZ-NaNTest", "sector": None, "market_cap": 3.0},
            {"ticker": "ZZTQ", "industry": "ZZ-NaNTest", "sector": None, "market_cap": 2.0},
            {"ticker": "ZZTR", "industry": "ZZ-NaNTest", "sector": None, "market_cap": 1.0},
        ]

    def fake_closes(tickers, as_of, window):
        # Content is irrelevant -- evaluate_pair is fully replaced below.
        return {t: [(as_of.isoformat(), 1.0)] for t in tickers}

    monkeypatch.setattr(ps, "_fetch_active_universe", fake_universe)
    monkeypatch.setattr(ps, "_load_bucket_closes", fake_closes)
    monkeypatch.setattr(ps, "_load_cost_bps", lambda: {})
    monkeypatch.setattr(ps, "pearson_prefilter", lambda *a, **k: (True, 0.9))

    # PQ: tiny finite p (must survive into the pool). QR: NaN eg_pvalue --
    # the case under test, must be dropped BEFORE bh_fdr. PR: a middling
    # finite p, used to prove n really did shrink to 2 (not 3) after the
    # NaN pair is excluded.
    canned_p = {
        frozenset({"ZZTP", "ZZTQ"}): 0.001,
        frozenset({"ZZTQ", "ZZTR"}): float("nan"),
        frozenset({"ZZTP", "ZZTR"}): 0.05,
    }

    def fake_evaluate_pair(t1, c1, t2, c2, as_of, window, min_obs_frac, cost_bps_map, cost_k):
        return {
            "ticker_a": t1, "ticker_b": t2, "beta": 1.0, "alpha": 0.0,
            "half_life_days": 10.0, "sigma_spread": 0.02, "spread_mean": 0.0,
            "eg_pvalue": canned_p[frozenset({t1, t2})], "cost_ok": True, "n_obs": 100,
        }

    monkeypatch.setattr(ps, "evaluate_pair", fake_evaluate_pair)

    summary = ps.run_scan(as_of=as_of, window=100, min_corr=0.6, fdr_q_threshold=0.10,
                           cost_k=2.0, out_path=str(out_path))

    assert summary["pairs_tested"] == 2, "the NaN pair must not count toward n"
    assert summary["errors_dropped"] == 1

    df = pd.read_parquet(out_path)
    assert len(df) == 2
    qr_present = (
        ((df["ticker_a"] == "ZZTQ") & (df["ticker_b"] == "ZZTR"))
        | ((df["ticker_a"] == "ZZTR") & (df["ticker_b"] == "ZZTQ"))
    ).any()
    assert not qr_present, "the NaN-eg_pvalue pair must not be written to the ledger"

    # With n=2 (NOT 3): PQ q = 0.001*2/1 = 0.002; PR q = max(0.002, 0.05*2/2) = 0.05.
    # If the NaN pair had leaked into the pool (n=3), PQ's q would instead be
    # 0.001*3/1=0.003 and PR's 0.05*3/2=0.075 -- this pins the n=2 behavior.
    pq_row = df[
        (df["ticker_a"].isin(["ZZTP", "ZZTQ"])) & (df["ticker_b"].isin(["ZZTP", "ZZTQ"]))
    ].iloc[0]
    pr_row = df[
        (df["ticker_a"].isin(["ZZTP", "ZZTR"])) & (df["ticker_b"].isin(["ZZTP", "ZZTR"]))
    ].iloc[0]
    assert pq_row["fdr_q"] == pytest.approx(0.002, rel=1e-9)
    assert pr_row["fdr_q"] == pytest.approx(0.05, rel=1e-9)


# ── item 2: canonical unordered pair key for dedupe + persistence ──────────
def test_direction_flip_rescan_dedupes_and_persists_on_canonical_key(tmp_path, monkeypatch):
    """Same as_of scanned twice with the EG direction flipped (ticker_a/
    ticker_b swapped between runs) must not double-persist under two
    'different' ordered keys: exactly one row must survive for the pair, and
    the FOLLOWING week's persistence lookup must see the newer (second)
    scan's fdr_pass, canonical-key-matched regardless of which direction
    labeled ticker_a/ticker_b in either scan."""
    out_path = tmp_path / "pair_ledger.parquet"
    as_of_1 = dt.date(2026, 8, 10)
    as_of_2 = dt.date(2026, 8, 17)

    def fake_universe(uri=None, table=None):
        return [
            {"ticker": "ZZTX", "industry": "ZZ-Flip", "sector": None, "market_cap": 2.0},
            {"ticker": "ZZTY", "industry": "ZZ-Flip", "sector": None, "market_cap": 1.0},
        ]

    def fake_closes(tickers, as_of, window):
        return {t: [(as_of.isoformat(), 1.0)] for t in tickers}

    monkeypatch.setattr(ps, "_fetch_active_universe", fake_universe)
    monkeypatch.setattr(ps, "_load_bucket_closes", fake_closes)
    monkeypatch.setattr(ps, "_load_cost_bps", lambda: {})
    monkeypatch.setattr(ps, "pearson_prefilter", lambda *a, **k: (True, 0.9))

    def make_canned(ticker_a, ticker_b, eg_pvalue):
        def fake_evaluate_pair(t1, c1, t2, c2, *a, **k):
            return {
                "ticker_a": ticker_a, "ticker_b": ticker_b, "beta": 1.0, "alpha": 0.0,
                "half_life_days": 10.0, "sigma_spread": 0.02, "spread_mean": 0.0,
                "eg_pvalue": eg_pvalue, "cost_ok": True, "n_obs": 100,
            }
        return fake_evaluate_pair

    # Scan 1 of as_of_1: EG direction X<-Y, tiny p -> fdr_pass True.
    monkeypatch.setattr(ps, "evaluate_pair", make_canned("ZZTX", "ZZTY", 0.001))
    ps.run_scan(as_of=as_of_1, window=100, min_corr=0.6, fdr_q_threshold=0.10,
                cost_k=2.0, out_path=str(out_path))
    df1 = pd.read_parquet(out_path)
    assert len(df1) == 1
    assert bool(df1.iloc[0]["fdr_pass"]) is True

    # Scan 2: RERUN of the SAME as_of_1, EG direction flips (Y<-X), and this
    # time the pair does NOT pass FDR.
    monkeypatch.setattr(ps, "evaluate_pair", make_canned("ZZTY", "ZZTX", 0.90))
    ps.run_scan(as_of=as_of_1, window=100, min_corr=0.6, fdr_q_threshold=0.10,
                cost_k=2.0, out_path=str(out_path))
    df2 = pd.read_parquet(out_path)
    assert len(df2) == 1, "exactly one row must survive the direction-flipped rerun"
    assert bool(df2.iloc[0]["fdr_pass"]) is False, "the newer (second) scan's result must win"

    # Next week: the persistence lookup must see the SECOND scan's fdr_pass
    # (False) for this pair -> approved must be False even though THIS
    # week's own fdr_pass is True (persistence requires both).
    monkeypatch.setattr(ps, "evaluate_pair", make_canned("ZZTX", "ZZTY", 0.001))
    ps.run_scan(as_of=as_of_2, window=100, min_corr=0.6, fdr_q_threshold=0.10,
                cost_k=2.0, out_path=str(out_path))
    df3 = pd.read_parquet(out_path)
    latest = df3[df3["as_of"] == as_of_2].iloc[0]
    assert bool(latest["fdr_pass"]) is True
    assert bool(latest["approved"]) is False


# ── item 3: replace-on-rescan must erase a stale as_of on a zero-pair rerun ─
def test_replace_on_rescan_zero_pairs_erases_stale_as_of(tmp_path, monkeypatch):
    """pair_ledger is DERIVED data (rebuildable from prices): rescanning an
    as_of that previously had surviving pairs, but now finds ZERO, must
    ERASE the stale rows for that as_of rather than leaving them standing."""
    out_path = tmp_path / "pair_ledger.parquet"
    as_of = dt.date(2026, 8, 10)
    n = 150

    def panel_for(_as_of):
        return _build_panel(as_of, n, seed=99)

    _install_fakes(monkeypatch, panel_for,
                    cost_bps={"ZZTA": 1.0, "ZZTB": 1.0, "ZZTC": 1.0, "ZZTD": 1.0, "ZZTE": 1.0})

    summary1 = ps.run_scan(as_of=as_of, window=100, min_corr=0.6, fdr_q_threshold=0.10,
                            cost_k=2.0, out_path=str(out_path), corr_lookback_days=90,
                            min_obs_frac=0.9)
    assert summary1["pairs_tested"] >= 1
    df1 = pd.read_parquet(out_path)
    assert (df1["as_of"] == as_of).sum() >= 1

    # Rescan the SAME as_of with min_corr set above the maximum possible
    # correlation (1.0) -> deterministically zero pairs survive the prefilter.
    summary2 = ps.run_scan(as_of=as_of, window=100, min_corr=1.01, fdr_q_threshold=0.10,
                            cost_k=2.0, out_path=str(out_path), corr_lookback_days=90,
                            min_obs_frac=0.9)
    assert summary2["pairs_tested"] == 0
    assert summary2["approved"] == 0

    df2 = pd.read_parquet(out_path)
    assert (df2["as_of"] == as_of).sum() == 0, "stale rows for the rescanned as_of must be erased"


# ── item 4: coint() exceptions are counted (errors_dropped) + WARN-logged ──
def test_coint_exception_counted_as_errors_dropped_and_warns(tmp_path, monkeypatch, caplog):
    """A pair whose coint() call raises must (a) make evaluate_pair return
    the _COINT_ERROR sentinel rather than silently swallowing it as None,
    (b) be counted in run_scan's errors_dropped, and (c) trigger one WARN
    log line for the scan."""
    import statsmodels.tsa.stattools as stattools

    def boom(*a, **k):
        raise RuntimeError("synthetic coint() failure")

    monkeypatch.setattr(stattools, "coint", boom)

    as_of = dt.date(2026, 8, 10)
    dates = [as_of - dt.timedelta(days=i) for i in range(20)][::-1]
    date_strs = [d.isoformat() for d in dates]
    dated_a = list(zip(date_strs, [100.0 + i * 0.1 for i in range(20)]))
    dated_b = list(zip(date_strs, [50.0 + i * 0.1 for i in range(20)]))

    # Unit-level: evaluate_pair itself must return the sentinel, not None.
    result = ps.evaluate_pair("ZZTM", dated_a, "ZZTN", dated_b, as_of, window=20,
                               min_obs_frac=0.9, cost_bps_map={}, cost_k=2.0)
    assert result is ps._COINT_ERROR

    # Full run_scan level: errors_dropped counts it, pairs_tested stays 0,
    # and a WARN is logged.
    out_path = tmp_path / "pair_ledger.parquet"

    def fake_universe(uri=None, table=None):
        return [
            {"ticker": "ZZTM", "industry": "ZZ-CointErr", "sector": None, "market_cap": 2.0},
            {"ticker": "ZZTN", "industry": "ZZ-CointErr", "sector": None, "market_cap": 1.0},
        ]

    def fake_closes(tickers, as_of, window):
        return {"ZZTM": dated_a, "ZZTN": dated_b}

    monkeypatch.setattr(ps, "_fetch_active_universe", fake_universe)
    monkeypatch.setattr(ps, "_load_bucket_closes", fake_closes)
    monkeypatch.setattr(ps, "_load_cost_bps", lambda: {})
    monkeypatch.setattr(ps, "pearson_prefilter", lambda *a, **k: (True, 0.9))

    with caplog.at_level("WARNING"):
        summary = ps.run_scan(as_of=as_of, window=20, min_corr=0.6, fdr_q_threshold=0.10,
                               cost_k=2.0, out_path=str(out_path), min_obs_frac=0.9)

    assert summary["errors_dropped"] == 1
    assert summary["pairs_tested"] == 0
    assert any("dropped" in r.message and "1" in r.message for r in caplog.records)


# ── item 7: FMP-profile-cache sector/industry backfill (pure function) ─────
def test_fill_missing_sector_industry_from_fmp_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "fmp_profile.json"
    cache_path.write_text(json.dumps({
        "ZZTFILL": {"sector": "ZZ-Sector", "industry": "ZZ-Industry",
                    "_fetched_at": "2026-08-24T00:00:00+00:00"},
        "ZZTOMB": {"_empty": True, "_fetched_at": "2026-08-24T00:00:00+00:00"},
    }))
    monkeypatch.setattr(ps, "FMP_PROFILE_CACHE_PATH", str(cache_path))

    rows = [
        {"ticker": "ZZTFILL", "industry": None, "sector": None, "market_cap": None},
        {"ticker": "ZZTOMB", "industry": None, "sector": None, "market_cap": None},
        {"ticker": "ZZTALREADY", "industry": "ZZ-Existing", "sector": None, "market_cap": None},
        {"ticker": "ZZTMISSING", "industry": None, "sector": None, "market_cap": None},
    ]
    ps._fill_missing_sector_industry_from_fmp_cache(rows)
    by_ticker = {r["ticker"]: r for r in rows}

    assert by_ticker["ZZTFILL"]["industry"] == "ZZ-Industry"
    assert by_ticker["ZZTFILL"]["sector"] == "ZZ-Sector"
    assert by_ticker["ZZTOMB"]["industry"] is None  # tombstone: nothing to fill
    assert by_ticker["ZZTALREADY"]["industry"] == "ZZ-Existing"  # untouched, not overwritten
    assert by_ticker["ZZTMISSING"]["industry"] is None  # no cache entry at all


# ── item 7: empty pinned `universe` table auto-falls-back to universe_config ─
def test_fetch_active_universe_falls_back_to_universe_config_when_empty(monkeypatch, caplog):
    class FakeCursor:
        def __init__(self):
            self.mode = None

        def execute(self, sql, params=None):
            if "information_schema.columns" in sql:
                self.mode = "cols"
            elif "universe_config" in sql:
                self.mode = "config"
            else:
                self.mode = "universe"

        def fetchall(self):
            if self.mode == "cols":
                return []  # no liquidity column discovered on either table
            if self.mode == "universe":
                return []  # pinned `universe` table: 0 active rows
            if self.mode == "config":
                return [("ZZTCFG", "ZZ-Industry", "ZZ-Sector", None)]
            return []

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(psycopg2, "connect", lambda uri: FakeConn())
    monkeypatch.setattr(ps, "_load_fmp_profile_cache", lambda: {})  # isolate from the real cache

    with caplog.at_level("WARNING"):
        rows = ps._fetch_active_universe(uri="postgresql://fake", table="universe")

    assert rows == [{"ticker": "ZZTCFG", "industry": "ZZ-Industry", "sector": "ZZ-Sector", "market_cap": None}]
    assert any("falling back" in r.message for r in caplog.records)


# ── item 9: missing cost-bps file logs a distinct WARN ─────────────────────
def test_load_cost_bps_missing_file_logs_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(ps, "COST_BPS_PATH", str(tmp_path / "does_not_exist.json"))
    with caplog.at_level("WARNING"):
        result = ps._load_cost_bps()
    assert result == {}
    assert any("cost-bps file" in r.message and "not found" in r.message for r in caplog.records)
