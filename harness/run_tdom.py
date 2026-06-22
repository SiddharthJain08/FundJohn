"""Orchestrator for the Ensemble Exit Policy T-DOM harness.

Wires clusters -> inputs -> {ensemble + 3 baselines} -> daily multi-day
first-touch replay -> growth G -> day-block bootstrap CI, and adjudicates the
spec's T-DOM gate (does the ensemble beat BOTH rejected baselines with a CI
lower bound > 0, net of cost).

Load-bearing wiring invariants (the parts the plan delegates to "the
orchestrator adapts"):

  (1) Index-aligned policy lists. For every kept cluster, all FOUR policies
      (ensemble + 3 baselines) get exactly one per-trade record, appended in
      lockstep and tagged with the SAME ``day``. If any input is invalid (nan
      ATR, empty/misaligned price slice, < 2 eligible legs, generator failure)
      the WHOLE cluster is skipped for all four -- the bootstrap pairs A[i]/B[i]
      by index, so a single missing entry on one side silently corrupts the CI.
  (2) Entry-bar alignment. The price slice spans [entry_day - lookback, entry_day
      + H_max]. ATR(20) is computed on the BACKWARD view ``df.loc[:entry_day]``
      (as_of=entry_day); replay walks the FORWARD view ``df.loc[entry_bar:]``
      whose row 0 is the entry/fill bar (first trading bar >= entry_day) and
      whose excursions start at row 1. ``entry`` price is the cluster anchor
      (execution_signals.entry_price), NOT close[0].
  (3) One ATR per cluster, threaded to both sides: ``sigma_underlying=atr``
      (generator level math) and ``sigma_ret = atr/entry`` (growth's
      R_i = R_ret/sigma_ret -> sigma units). Same atr, same entry.
  (4) Carry to both generate AND replay (shorts / tiered arm): build_context
      sets ctx.carry_per_bar; the same value is passed to first_touch_multiday.
  (5) Eligible-prune divergence from combine_backtest is logged (legs dropped).
  (6) Wrong-side bracket legs are counted/logged (abs() distances accept them).

Adapter (per plan Self-Review): replay returns key ``R`` (signed return-on-
entry) which we store as ``R_ret`` and pair with ``sigma_ret = atr/entry``.
"""
import os
import json
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, "/root/openclaw/src")

import inputs as I          # noqa: E402
import baselines as B       # noqa: E402
import generator as G       # noqa: E402
import prices as P          # noqa: E402
import growth as GR         # noqa: E402
import replay as RP         # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")
# Calendar-days back from window_start for ATR(20). Generous (200d) so >= 20
# TRADING bars survive even across real data-coverage gaps in prices.parquet
# (e.g. AAPL is missing all of 2026-03 -> a 60d lookback starves ATR). ATR uses
# the last 20 AVAILABLE bars up to entry (tail(20)), so a deeper window only
# guarantees enough bars; it does not change which 20 are used.
LOOKBACK_BARS = 200
DEFAULT_H_MAX = 30
DEFAULT_TXN_COST_SIGMA = 0.02

# the four policies, in a fixed order; key -> ("ensemble"|callable spec)
POLICY_KEYS = ("ensemble", "min_stop_cumulative", "conf_weighted_atr", "current_live_v2")
BASELINE_GATE_KEYS = ("min_stop_cumulative", "conf_weighted_atr")  # formal T-DOM gate


# --------------------------------------------------------------------------- #
# regime-table cache
# --------------------------------------------------------------------------- #
def _load_regime_tables(regimes):
    """regime -> {weights, sharpe, cadence, matrix} from the live tables."""
    from execution import strategy_weights as sw
    from execution import strategy_similarity as ss
    out = {}
    for reg in regimes:
        rows = sw.load_current(reg)
        try:
            matrix = ss.load_groups(reg)["matrix"]
        except Exception:
            matrix = {}
        out[reg] = dict(
            weights={r["strategy_id"]: float(r["daily_weight"]) for r in rows},
            sharpe={r["strategy_id"]: float(r["effective_sharpe"]) for r in rows},
            cadence={r["strategy_id"]: float(r["cadence_days"]) for r in rows},
            matrix=matrix,
        )
    return out


def _wrong_side_count(cluster):
    """Count legs whose stop/target sit on the wrong side of entry for the dir."""
    d = int(cluster.direction)
    entry = float(cluster.entry)
    bad = 0
    for leg in cluster.legs:
        stop, tgt = float(leg["stop_loss"]), float(leg["target_1"])
        # long: stop < entry < target; short: target < entry < stop
        if d == 1 and not (stop < entry < tgt):
            bad += 1
        elif d == -1 and not (tgt < entry < stop):
            bad += 1
    return bad


# --------------------------------------------------------------------------- #
# floor-pin probe (Step 0)
# --------------------------------------------------------------------------- #
def floor_pin_probe(clusters, regime_tables, conn, n=50, seed=0,
                    half_life_mode="autocorr", carry_mode="tiered",
                    a_grid=(0.5, 5.0, 0.1)):
    """Run generator.generate on up to ``n`` real clusters; report the fraction
    whose ensemble stop multiplier ``a_mult`` pins at the grid floor (a_grid[0]).

    Lazily loads + caches per-regime tables when ``regime_tables`` is None AND
    primes its OWN price cache for the probed tickers (so the standalone Task-9
    invocation works without a prior run()). Logged, not assumed -- decides
    whether per-cluster Monte Carlo is needed.
    """
    probe_clusters = clusters[:n]
    if not probe_clusters:
        return dict(frac_at_floor=float("nan"), evaluated=0, interior_examples=[])
    if regime_tables is None:
        regime_tables = _load_regime_tables({c.regime for c in probe_clusters})
    # prime the probe's own price cache (floor_pin_probe may run before run())
    _prime_prices({c.ticker for c in probe_clusters},
                  min(c.day for c in probe_clusters),
                  max(c.day for c in probe_clusters))
    cfg = e_config(seed=seed, a_grid=a_grid)
    floor = float(a_grid[0])
    ceiling = float(a_grid[1])
    at_floor = 0
    at_ceiling = 0
    noiseband_bound = 0   # noise-band floor (A-5, exit_sim line 238) overrode the grid pick
    a_mults = []
    interior = []
    evaluated = 0
    for c in clusters[:n]:
        atr = _atr_for(c)
        if not np.isfinite(atr) or atr <= 0:
            continue
        strs, ctx, ids = I.build_context(c, regime_tables, conn, half_life_mode,
                                         carry_mode, atr_value=atr)
        if len(ids) < 2:
            continue
        try:
            pol = G.generate(strs, ctx, cfg)
        except Exception:
            continue
        evaluated += 1
        am = pol["diagnostics"].get("a_mult")
        if am is None:
            continue
        am = float(am)
        a_mults.append(am)
        sig = pol["diagnostics"].get("sigma_eff")
        # noise-band floor binds when realized stop_dist > grid pick a_mult*sigma_eff
        # (exit_sim line 238: a = max(am*sigma, k_min*sigma*sqrt(tau_guess)))
        if sig is not None and sig > 0:
            if float(pol["stop_dist"]) > am * float(sig) * (1.0 + 1e-6):
                noiseband_bound += 1
        if abs(am - floor) < 1e-9:
            at_floor += 1
        elif abs(am - ceiling) < 1e-9:
            at_ceiling += 1
        if abs(am - floor) > 1e-9 and len(interior) < 10:
            interior.append(dict(day=c.day, ticker=c.ticker,
                                 direction=c.direction, a_mult=am))
    frac = (at_floor / evaluated) if evaluated else float("nan")
    a_mults = sorted(a_mults)
    am_summary = {}
    if a_mults:
        arr = np.array(a_mults, float)
        am_summary = dict(min=float(arr.min()), p25=float(np.percentile(arr, 25)),
                          median=float(np.median(arr)), p75=float(np.percentile(arr, 75)),
                          max=float(arr.max()), mean=float(arr.mean()))
    return dict(frac_at_floor=frac, evaluated=evaluated,
                frac_at_ceiling=(at_ceiling / evaluated) if evaluated else float("nan"),
                frac_noiseband_bound=(noiseband_bound / evaluated) if evaluated else float("nan"),
                a_mult_summary=am_summary, interior_examples=interior,
                mc_always_runs=True)


def e_config(seed=0, a_grid=(0.5, 5.0, 0.1), mc_paths=20_000, mc_dt=0.5):
    import exit_sim as e
    return e.Config(mc_paths=mc_paths, mc_dt=mc_dt, seed=seed, a_grid=a_grid)


# --------------------------------------------------------------------------- #
# price plumbing
# --------------------------------------------------------------------------- #
def _date_minus(iso, days):
    import datetime
    y, m, d = (int(x) for x in iso.split("-"))
    return (datetime.date(y, m, d) - datetime.timedelta(days=days)).isoformat()


def _date_plus(iso, days):
    import datetime
    y, m, d = (int(x) for x in iso.split("-"))
    return (datetime.date(y, m, d) + datetime.timedelta(days=days)).isoformat()


_PRICE_CACHE = {}   # ticker -> full sliced frame over the run window (per-ticker)


def _prime_prices(tickers, win_start, win_end):
    """Slice the panel ONCE per run for the cluster tickers (predicate pushdown).

    Window is [win_start - LOOKBACK, win_end + H_max-ish]; cached per ticker.
    """
    start = _date_minus(win_start, LOOKBACK_BARS + 10)
    end = _date_plus(win_end, DEFAULT_H_MAX + 10)
    frames = P.load_daily(set(tickers), start, end)
    _PRICE_CACHE.clear()
    _PRICE_CACHE.update(frames)


def _atr_for(cluster, n=20):
    df = _PRICE_CACHE.get(cluster.ticker)
    if df is None or len(df) == 0:
        return float("nan")
    back = df.loc[:cluster.day]
    return P.atr(back, n=n, as_of=cluster.day)


def _forward_bars(cluster, h_max):
    """Forward price view whose row 0 is the entry/fill bar (first bar >= day).

    Returns None when no bar at-or-after the entry day, or the slice is too
    short (< 2 bars -> no excursion possible).
    """
    df = _PRICE_CACHE.get(cluster.ticker)
    if df is None or len(df) == 0:
        return None
    fwd = df.loc[cluster.day:]
    if len(fwd) < 2:
        return None
    # cap to entry-bar + h_max bars (entry-bar is row 0)
    return fwd.iloc[: h_max + 1]


# --------------------------------------------------------------------------- #
# main run
# --------------------------------------------------------------------------- #
def _policies_for_cluster(cluster, strategies, ctx, ids, regime_tables, atr, cfg,
                          H_max=DEFAULT_H_MAX):
    """Build the four Policy dicts for one cluster (ensemble + 3 baselines).

    Returns dict policy_key -> Policy, or None if the ensemble generation fails.
    Baselines consume the pruned-leg cluster view (same eligible legs as the
    ensemble) so all four are scored on the SAME constituents.

    ``H_max`` (the run's shared max-hold cap, design Sec.7) is threaded to ALL
    THREE baselines so they ride to the SAME horizon as the ensemble -- not a
    hardcoded 30. The ensemble's native ``T_exit`` is additionally clamped at
    ``H_max`` so its effective horizon matches even if the forward price slice
    is later lengthened beyond H_max+1 bars (keeping the comparison apples-to-
    apples per Sec.7).
    """
    rt = regime_tables[cluster.regime]
    weights = {sid: rt["weights"].get(sid, 0.0) for sid in ids}
    sharpe = {sid: rt["sharpe"].get(sid, 0.0) for sid in ids}

    # pruned cluster view: only the eligible legs (same set the ensemble used)
    kept_legs = [leg for leg in cluster.legs if leg["strategy_id"] in set(ids)]
    pruned = I.Cluster(day=cluster.day, ticker=cluster.ticker,
                       direction=cluster.direction, entry=cluster.entry,
                       legs=kept_legs, regime=cluster.regime,
                       easy_to_borrow=cluster.easy_to_borrow)

    try:
        ens = G.generate(strategies, ctx, cfg)
    except Exception:
        return None
    h_max = int(H_max)
    ens["time_stop_bars"] = min(float(ens["time_stop_bars"]), float(h_max))
    return {
        "ensemble": ens,
        "min_stop_cumulative": B.min_stop_cumulative(pruned, weights, H_max=h_max),
        "conf_weighted_atr": B.conf_weighted_atr(pruned, weights, sharpe, atr, H_max=h_max),
        "current_live_v2": B.current_live_v2(pruned, weights, H_max=h_max),
    }


def run(window_start="2026-05-04", half_life_mode="autocorr", carry_mode="tiered",
        n_clusters=None, txn_cost_sigma=DEFAULT_TXN_COST_SIGMA, H_max=DEFAULT_H_MAX,
        seed=0, conn=None, clusters=None, regime_tables=None, n_boot=2000,
        sample=None, mc_paths=20_000, dump_trades=None):
    """Full T-DOM pipeline for one (half_life_mode, carry_mode) arm.

    Returns a dict with per-trade counts, per-policy G, and ΔG+CI of the
    ensemble vs each baseline, split long / short / combined.

    ``sample``: if set, draw a STRIDED subsample of this many clusters spread
    across the full (day-sorted) set -- a representative tiny slice that spans
    days/regimes/sides, unlike a same-day ``n_clusters`` head (the smoke uses
    this so the eligible-prune still clears enough distinct-day trades for the
    block bootstrap). ``n_clusters`` (head slice) still applies first if given.
    """
    own_conn = False
    if conn is None:
        import psycopg2
        from dotenv import load_dotenv
        load_dotenv("/root/openclaw/.env")
        conn = psycopg2.connect(os.environ["POSTGRES_URI"])
        own_conn = True

    try:
        if clusters is None:
            clusters = I.extract_clusters(conn, window_start=window_start)
        if n_clusters is not None:
            clusters = clusters[:n_clusters]
        if sample is not None and sample < len(clusters):
            stride = max(1, len(clusters) // sample)
            clusters = clusters[::stride][:sample]
        if regime_tables is None:
            regime_tables = _load_regime_tables({c.regime for c in clusters})

        # one panel slice for the run (predicate pushdown), cached per ticker
        win_end = max((c.day for c in clusters), default=window_start)
        _prime_prices({c.ticker for c in clusters}, window_start, win_end)

        cfg = e_config(seed=seed, mc_paths=mc_paths)

        # per-trade record lists, kept index-aligned across all four policies
        recs = {k: [] for k in POLICY_KEYS}
        side = []   # +1 / -1 per kept trade (parallel to recs lists)
        skip = dict(no_atr=0, bad_slice=0, few_legs=0, gen_fail=0)
        wrong_side_legs = 0
        eligible_drops = 0
        kept = 0

        for c in clusters:
            atr = _atr_for(c)
            if not np.isfinite(atr) or atr <= 0:
                skip["no_atr"] += 1
                continue
            strategies, ctx, ids = I.build_context(
                c, regime_tables, conn, half_life_mode, carry_mode, atr_value=atr)
            eligible_drops += (len(c.legs) - len(ids))
            if len(ids) < 2:
                skip["few_legs"] += 1
                continue
            bars = _forward_bars(c, H_max)
            if bars is None:
                skip["bad_slice"] += 1
                continue

            pols = _policies_for_cluster(c, strategies, ctx, ids, regime_tables, atr, cfg,
                                         H_max=H_max)
            if pols is None:
                skip["gen_fail"] += 1
                continue

            carry = float(getattr(ctx, "carry_per_bar", 0.0))
            sigma_ret = atr / float(c.entry)
            wrong_side_legs += _wrong_side_count(c)

            # invariant (1): replay all four; only commit if all four produce a
            # finite record, and append in lockstep tagged with the same day.
            trades = {}
            ok = True
            for k in POLICY_KEYS:
                o = RP.first_touch_multiday(pols[k], bars, float(c.entry),
                                            carry_per_bar=carry)
                if not np.isfinite(o["R"]) or not np.isfinite(sigma_ret) or sigma_ret <= 0:
                    ok = False
                    break
                trades[k] = dict(day=c.day, R_ret=float(o["R"]), tau=float(o["tau"]),
                                 sigma_ret=float(sigma_ret),
                                 stop_dist_frac=float(pols[k]["stop_dist"]) / float(c.entry),
                                 exit_kind=o["exit_kind"],
                                 frac_filled=float(o["frac_filled"]))
            if not ok:
                skip["gen_fail"] += 1
                continue
            for k in POLICY_KEYS:
                recs[k].append(trades[k])
            side.append(int(c.direction))
            kept += 1

        side = np.array(side, int)
        if dump_trades:
            import json as _json
            with open(dump_trades, "w") as _f:
                _json.dump(dict(recs=recs, side=side.tolist(),
                                arm=dict(half_life_mode=half_life_mode, carry_mode=carry_mode)),
                           _f)
        result = dict(
            arm=dict(half_life_mode=half_life_mode, carry_mode=carry_mode),
            window_start=window_start, H_max=int(H_max),
            txn_cost_sigma=float(txn_cost_sigma), seed=int(seed),
            n_clusters=len(clusters), n_trades=int(kept),
            skipped=skip, eligible_leg_drops=int(eligible_drops),
            wrong_side_legs=int(wrong_side_legs),
        )

        for split, mask in (("combined", np.ones(kept, bool)),
                            ("long", side == 1),
                            ("short", side == -1)):
            idx = np.where(mask)[0]
            sub = {k: [recs[k][i] for i in idx] for k in POLICY_KEYS}
            g_by_policy = {k: GR.growth_G(sub[k]) for k in POLICY_KEYS}
            block = dict(n=int(len(idx)), G=g_by_policy,
                         distinct_days=len({t["day"] for t in sub["ensemble"]}),
                         exit_kinds=_exit_kind_dist(sub["ensemble"]),
                         per_policy=_per_policy_dist(sub))
            for base in ("min_stop_cumulative", "conf_weighted_atr", "current_live_v2"):
                if len(idx) >= 2 and len({t["day"] for t in sub["ensemble"]}) >= 2:
                    block[base] = GR.bootstrap_delta(sub["ensemble"], sub[base],
                                                     n_boot=n_boot, seed=seed)
                else:
                    block[base] = dict(delta=float("nan"), lo=float("nan"),
                                       hi=float("nan"), p_gt0=float("nan"))
            block["tdom_pass"] = bool(
                all(np.isfinite(block[b]["lo"]) and block[b]["lo"] > 0
                    for b in BASELINE_GATE_KEYS))
            result[split] = block
        return result
    finally:
        if own_conn:
            conn.close()


def _exit_kind_dist(trades):
    from collections import Counter
    c = Counter(t["exit_kind"] for t in trades)
    n = max(len(trades), 1)
    return {k: round(v / n, 4) for k, v in c.items()}


def _per_policy_dist(sub):
    """For each policy: exit_kind distribution + mean R (return-on-entry) + mean tau
    + mean frac_filled. Lets the report show the mechanism behind any G gap."""
    out = {}
    for k in POLICY_KEYS:
        tr = sub[k]
        n = max(len(tr), 1)
        out[k] = dict(
            mean_R_ret=float(np.mean([t["R_ret"] for t in tr])) if tr else float("nan"),
            mean_tau=float(np.mean([t["tau"] for t in tr])) if tr else float("nan"),
            mean_frac_filled=float(np.mean([t["frac_filled"] for t in tr])) if tr else float("nan"),
            exit_kinds=_exit_kind_dist(tr),
        )
    return out


# --------------------------------------------------------------------------- #
# matrix + report
# --------------------------------------------------------------------------- #
def main(window_start="2026-05-04", n_clusters=None, seed=0, n_boot=2000,
         sample=None):
    """Run the full {autocorr,cadence} x {tiered,zero} matrix.

    ``sample``: strided subsample cap (memory/time bound, NOT silent truncation
    -- logged in the report). The SAME strided cluster set drives the floor-pin
    probe and every arm so the four arms and the probe are directly comparable.
    A strided draw spans all distinct days/regimes/sides (a head ``n_clusters``
    slice would day-starve the day-block bootstrap).
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv("/root/openclaw/.env")
    conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        full = I.extract_clusters(conn, window_start=window_start)
        if n_clusters is not None:
            full = full[:n_clusters]
        n_full = len(full)
        if sample is not None and sample < len(full):
            stride = max(1, len(full) // sample)
            clusters = full[::stride][:sample]
        else:
            clusters = full
        regime_tables = _load_regime_tables({c.regime for c in clusters})

        cap_info = dict(n_full_clusters=n_full, n_after_cap=len(clusters),
                        sample_cap=sample,
                        distinct_days_full=len({c.day for c in full}),
                        distinct_days_capped=len({c.day for c in clusters}))

        probe = floor_pin_probe(clusters, regime_tables, conn, n=50, seed=seed)
        probe["cap_info"] = cap_info
        with open(os.path.join(OUT_DIR, "floor_pin.json"), "w") as f:
            json.dump(probe, f, indent=2)

        arms = [("autocorr", "tiered"), ("autocorr", "zero"),
                ("cadence", "tiered"), ("cadence", "zero")]
        results = {}
        for hl_mode, carry_mode in arms:
            res = run(window_start=window_start, half_life_mode=hl_mode,
                      carry_mode=carry_mode, seed=seed, conn=conn,
                      clusters=clusters, regime_tables=regime_tables, n_boot=n_boot)
            res["cap_info"] = cap_info
            key = f"{hl_mode}_{carry_mode}"
            results[key] = res
            with open(os.path.join(OUT_DIR, f"tdom_{key}.json"), "w") as f:
                json.dump(res, f, indent=2)

        _write_report(results, probe, cap_info)
        return dict(probe=probe, results=results, cap_info=cap_info)
    finally:
        conn.close()


def _write_report(results, probe, cap_info=None):
    primary = "autocorr_tiered"
    pr = results.get(primary, {})
    lines = ["# Ensemble Exit Policy — T-DOM Results", ""]

    # ---- headline verdict (primary arm) ------------------------------------ #
    lines.append("## Headline verdict (PRIMARY arm = autocorr half-life, tiered carry)")
    lines.append("T-DOM gate: ADOPT iff the 95% block-bootstrap CI lower bound of "
                 "ΔG (ensemble − baseline) is **strictly > 0** vs BOTH gate baselines "
                 "(min_stop_cumulative AND conf_weighted_atr). Never a bare point estimate.")
    lines.append("")
    for split in ("combined", "long", "short"):
        blk = pr.get(split, {})
        msc = blk.get("min_stop_cumulative", {})
        cwa = blk.get("conf_weighted_atr", {})
        verdict = "ADOPT" if blk.get("tdom_pass") else "REJECT / INCONCLUSIVE"
        lines.append(
            f"- **{split.upper()}** (n={blk.get('n')}): **{verdict}**. "
            f"ΔG vs min_stop: {_fmt(msc.get('delta'))} CI[{_fmt(msc.get('lo'))}, {_fmt(msc.get('hi'))}]; "
            f"ΔG vs conf_atr: {_fmt(cwa.get('delta'))} CI[{_fmt(cwa.get('lo'))}, {_fmt(cwa.get('hi'))}]. "
            f"Gate needs BOTH lo>0.")
    lines.append("")

    # ---- run-size / cap ----------------------------------------------------- #
    if cap_info:
        lines.append("## Sample / cap (NO silent truncation)")
        lines.append(
            f"- Full clusters in window: {cap_info['n_full_clusters']} "
            f"({cap_info['distinct_days_full']} distinct days). "
            f"Strided cap (sample={cap_info['sample_cap']}) → {cap_info['n_after_cap']} clusters "
            f"({cap_info['distinct_days_capped']} distinct days).")
        lines.append(
            "- The day-block bootstrap resamples DISTINCT DAYS; CI width is governed by the "
            "distinct-day count, not the trade count. Strided (not head) sampling preserves all days.")
        lines.append(f"- Kept trades per arm: " +
                     ", ".join(f"{k}={v['n_trades']}" for k, v in results.items()))
        lines.append("")

    # ---- floor-pin / MC finding -------------------------------------------- #
    am = probe.get("a_mult_summary", {})
    lines.append("## Floor-pin probe (Step 0) — deterministic-levels finding")
    lines.append(
        f"- MC ran on EVERY cluster (mc_paths=20000): the design's planned skip-MC-when-floor-pinned "
        f"optimization was NOT wired into the reference generator (`exit_sim.optimize_stop` always "
        f"sweeps the full a_grid × Monte Carlo). Results stay correct; the design-vs-impl gap is noted.")
    lines.append(
        f"- a_mult (grid argmax multiplier) distribution over {probe.get('evaluated')} probed clusters: "
        f"min={_fmt(am.get('min'))} p25={_fmt(am.get('p25'))} median={_fmt(am.get('median'))} "
        f"p75={_fmt(am.get('p75'))} max={_fmt(am.get('max'))} mean={_fmt(am.get('mean'))}.")
    lines.append(
        f"- frac_at_grid_floor (a_mult==0.5)={_fmt(probe.get('frac_at_floor'))}; "
        f"frac_at_grid_ceiling (a_mult==5.0)={_fmt(probe.get('frac_at_ceiling'))}; "
        f"frac_noiseband_floor_bound (A-5 override of grid pick)={_fmt(probe.get('frac_noiseband_bound'))}.")
    lines.append(
        "- Interpretation: the grid argmax is bimodal (floor vs ceiling), NOT uniformly pinned at the "
        "noise-band floor as the design hypothesized — so MC genuinely engages and the levels are not "
        "trivially deterministic. Treat the a* selection as MC-driven, not floor-pinned.")
    lines.append("")

    # ---- per-arm detail ----------------------------------------------------- #
    lines.append("## Per-arm detail")
    for key, res in results.items():
        star = "  ⭐ PRIMARY" if key == primary else ""
        lines.append(f"### arm: {key}{star}")
        lines.append(f"- n_trades={res['n_trades']} (skipped {res['skipped']}); "
                     f"eligible_leg_drops={res['eligible_leg_drops']}; "
                     f"wrong_side_legs={res['wrong_side_legs']}")
        for split in ("combined", "long", "short"):
            blk = res.get(split, {})
            g = blk.get("G", {})
            lines.append(f"#### {split} (n={blk.get('n')}, distinct_days={blk.get('distinct_days')})")
            lines.append("  G: " + ", ".join(f"{k}={_fmt(g.get(k))}" for k in POLICY_KEYS))
            for base in ("min_stop_cumulative", "conf_weighted_atr", "current_live_v2"):
                d = blk.get(base, {})
                tag = "(GATE)" if base in BASELINE_GATE_KEYS else "(info)"
                lines.append(f"  ΔG vs {base} {tag}: delta={_fmt(d.get('delta'))} "
                             f"CI[{_fmt(d.get('lo'))}, {_fmt(d.get('hi'))}] "
                             f"p>0={_fmt(d.get('p_gt0'))}")
            pp = blk.get("per_policy", {})
            lines.append("  mechanism (mean R_ret / mean tau / mean frac_filled):")
            for k in POLICY_KEYS:
                d = pp.get(k, {})
                lines.append(f"    {k}: R={_fmt(d.get('mean_R_ret'))} tau={_fmt(d.get('mean_tau'))} "
                             f"frac={_fmt(d.get('mean_frac_filled'))} exits={d.get('exit_kinds')}")
            lines.append(f"  **T-DOM pass: {blk.get('tdom_pass')}**")
        lines.append("")

    # ---- caveats ------------------------------------------------------------ #
    lines.append("## Caveats (decision-grade)")
    lines += [
        "- **Short carry is FABRICATED, not sourced.** Tiered GC(0.3%/yr)/HTB(5%/yr) keyed on the binary "
        "easy_to_borrow flag; no real borrow-rate or dividend data. At GC scale carry≈0 so shorts ≈ mirrored "
        "longs; carry only bites on genuine HTB names — exactly where the deferred A1–A4 (leverage-vol, "
        "skew/squeeze, recall, margin) asymmetries also matter. Do NOT read the short verdict as if borrow were measured.",
        "- **C is a BLEND**: Jaccard co-firing blended with return-correlation (mig 123), not pure Jaccard — "
        "the spec's well-conditioning argument is only approximately satisfied.",
        "- **effective_sharpe unit mix**: annualized backtest blended with per-trade live. Shared by all "
        "policies so it biases absolute mu0/G, not ΔG.",
        "- **Daily OHLC first-touch** can't see intrabar order; stop-wins-on-tie applied uniformly (conservative). "
        "Gap-through fills at the level.",
        "- **Selection / deflation**: 4 arms × 3 baselines × 3 splits compared — apply deflated-Sharpe-style "
        "caution; the gate's strict CI-lo>0 (not point estimate) is the deflation guard.",
        "- **Eligible-prune attrition is genuine**: ~40% of execution_signals legs come from strategies absent "
        "from the current regime weight table (deprecated/unapproved strategies still emit signals); those legs "
        "are dropped, mirroring the live sizer. Clusters falling below 2 eligible legs are skipped (few_legs).",
        "- **Adoption (live wiring) is OUT OF SCOPE** — a separate operator-gated change + restart.",
    ]
    with open(os.path.join(OUT_DIR, "REPORT.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _fmt(x):
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.5f}"
    except (TypeError, ValueError):
        return str(x)


if __name__ == "__main__":
    main()
