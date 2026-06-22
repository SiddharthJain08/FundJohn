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

    Lazily loads + caches per-regime tables when ``regime_tables`` is None (so
    the Task-9 standalone invocation works). Logged, not assumed -- decides
    whether per-cluster Monte Carlo is needed.
    """
    if regime_tables is None:
        regime_tables = _load_regime_tables({c.regime for c in clusters[:n] or clusters})
    cfg = e_config(seed=seed, a_grid=a_grid)
    floor = float(a_grid[0])
    at_floor = 0
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
        if abs(float(am) - floor) < 1e-9:
            at_floor += 1
        else:
            if len(interior) < 10:
                interior.append(dict(day=c.day, ticker=c.ticker,
                                     direction=c.direction, a_mult=float(am)))
    frac = (at_floor / evaluated) if evaluated else float("nan")
    return dict(frac_at_floor=frac, evaluated=evaluated, interior_examples=interior)


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
def _policies_for_cluster(cluster, strategies, ctx, ids, regime_tables, atr, cfg):
    """Build the four Policy dicts for one cluster (ensemble + 3 baselines).

    Returns dict policy_key -> Policy, or None if the ensemble generation fails.
    Baselines consume the pruned-leg cluster view (same eligible legs as the
    ensemble) so all four are scored on the SAME constituents.
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
    h_max = int(DEFAULT_H_MAX)
    return {
        "ensemble": ens,
        "min_stop_cumulative": B.min_stop_cumulative(pruned, weights, H_max=h_max),
        "conf_weighted_atr": B.conf_weighted_atr(pruned, weights, sharpe, atr, H_max=h_max),
        "current_live_v2": B.current_live_v2(pruned, weights, H_max=h_max),
    }


def run(window_start="2026-05-04", half_life_mode="autocorr", carry_mode="tiered",
        n_clusters=None, txn_cost_sigma=DEFAULT_TXN_COST_SIGMA, H_max=DEFAULT_H_MAX,
        seed=0, conn=None, clusters=None, regime_tables=None, n_boot=2000,
        sample=None):
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

        cfg = e_config(seed=seed)

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

            pols = _policies_for_cluster(c, strategies, ctx, ids, regime_tables, atr, cfg)
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
                                 sigma_ret=float(sigma_ret), exit_kind=o["exit_kind"],
                                 frac_filled=float(o["frac_filled"]))
            if not ok:
                skip["gen_fail"] += 1
                continue
            for k in POLICY_KEYS:
                recs[k].append(trades[k])
            side.append(int(c.direction))
            kept += 1

        side = np.array(side, int)
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
                         exit_kinds=_exit_kind_dist(sub["ensemble"]))
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


# --------------------------------------------------------------------------- #
# matrix + report
# --------------------------------------------------------------------------- #
def main(window_start="2026-05-04", n_clusters=None, seed=0, n_boot=2000):
    os.makedirs(OUT_DIR, exist_ok=True)
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv("/root/openclaw/.env")
    conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        clusters = I.extract_clusters(conn, window_start=window_start)
        if n_clusters is not None:
            clusters = clusters[:n_clusters]
        regime_tables = _load_regime_tables({c.regime for c in clusters})

        probe = floor_pin_probe(clusters, regime_tables, conn, n=50, seed=seed)
        with open(os.path.join(OUT_DIR, "floor_pin.json"), "w") as f:
            json.dump(probe, f, indent=2)

        arms = [("autocorr", "tiered"), ("autocorr", "zero"),
                ("cadence", "tiered"), ("cadence", "zero")]
        results = {}
        for hl_mode, carry_mode in arms:
            res = run(window_start=window_start, half_life_mode=hl_mode,
                      carry_mode=carry_mode, seed=seed, conn=conn,
                      clusters=clusters, regime_tables=regime_tables, n_boot=n_boot)
            key = f"{hl_mode}_{carry_mode}"
            results[key] = res
            with open(os.path.join(OUT_DIR, f"tdom_{key}.json"), "w") as f:
                json.dump(res, f, indent=2)

        _write_report(results, probe)
        return dict(probe=probe, results=results)
    finally:
        conn.close()


def _write_report(results, probe):
    lines = ["# Ensemble Exit Policy — T-DOM Results", ""]
    lines.append(f"Floor-pin probe: frac_at_floor={probe['frac_at_floor']:.3f} "
                 f"(evaluated {probe['evaluated']}); "
                 f"{'deterministic levels confirmed — MC not needed' if probe['frac_at_floor'] == 1.0 else 'some interior cases — MC fallback active'}.")
    lines.append("")
    primary = "autocorr_tiered"
    lines.append(f"**Primary arm = {primary}** (autocorr half-life, tiered carry). "
                 "T-DOM gate = CI lo>0 vs BOTH min_stop_cumulative AND conf_weighted_atr.")
    lines.append("")
    for key, res in results.items():
        star = "  ⭐ PRIMARY" if key == primary else ""
        lines.append(f"## arm: {key}{star}")
        lines.append(f"- n_trades={res['n_trades']} (skipped {res['skipped']}); "
                     f"eligible_leg_drops={res['eligible_leg_drops']}; "
                     f"wrong_side_legs={res['wrong_side_legs']}")
        for split in ("combined", "long", "short"):
            blk = res.get(split, {})
            g = blk.get("G", {})
            lines.append(f"### {split} (n={blk.get('n')})")
            lines.append("  G: " + ", ".join(f"{k}={_fmt(g.get(k))}" for k in POLICY_KEYS))
            for base in ("min_stop_cumulative", "conf_weighted_atr", "current_live_v2"):
                d = blk.get(base, {})
                lines.append(f"  ΔG vs {base}: delta={_fmt(d.get('delta'))} "
                             f"CI[{_fmt(d.get('lo'))}, {_fmt(d.get('hi'))}] "
                             f"p>0={_fmt(d.get('p_gt0'))}")
            lines.append(f"  exit_kinds(ensemble): {blk.get('exit_kinds')}")
            lines.append(f"  **T-DOM pass: {blk.get('tdom_pass')}**")
        lines.append("")
    lines.append("## Caveats")
    lines += [
        "- C is Jaccard-co-firing BLENDED with return-correlation, not pure Jaccard.",
        "- effective_sharpe mixes annualized backtest + per-trade live (shared by all policies; biases mu0 not ΔG).",
        "- Short carry is FABRICATED (tiered GC/HTB assumption; no borrow-rate/div data). On easy-to-borrow names carry≈0 so shorts≈mirrored longs; carry only bites on genuine HTB names (where deferred A1–A4 also matter). Do NOT read the short verdict as if borrow were measured.",
        "- Daily OHLC first-touch can't see intrabar order — stop-wins-on-tie applied uniformly (conservative).",
        "- Deflated-Sharpe caution: multiple arms × baselines compared.",
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
