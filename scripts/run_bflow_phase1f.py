#!/usr/bin/env python3
"""SP-6 B-flow Phase-1f — Intraday Drift Atlas runner (PRE-REGISTERED).

Spec (BINDING): docs/superpowers/specs/2026-06-07-sp6-bflow-phase1f-drift-atlas-prereg.md
Single session-major pass over a minute-bar cache dir (CACHE-ONLY, never
fetches). Per session: common drift curves (gross, net, cost) + deviation
from the uniform-accrual null. Pooled clustered-t verdict. Zero free parameters.

Usage:
    PYTHONPATH=src:. python3 scripts/run_bflow_phase1f.py \\
        --cache-dir data/cache/min_bars_hist \\
        --analysis-dir analysis [--limit N]

Run DETACHED (systemd-run) to survive session exits.
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "src"), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main(argv=None):
    p = argparse.ArgumentParser(prog="run_bflow_phase1f")
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--analysis-dir", required=True)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args([] if argv is None else argv)

    import numpy as np
    import pandas as pd
    from research.bflow.drift_atlas import (
        session_curves, pooled_stats, verdict, named_shapes,
        bucket_curves, TEST_MINUTES, N_XS_MIN, MIN_VALID_MINUTES,
        MIN_SESSIONS, T_PASS,
    )
    from research.bflow.run_phase1b import (enumerate_cache_sessions,
                                            load_session_frame,
                                            _ticker_frames)

    sessions = enumerate_cache_sessions(args.cache_dir)
    if args.limit is not None:
        sessions = sessions[:args.limit]
    print(f"[bflow-p1f] cache {args.cache_dir}: {len(sessions)} sessions "
          f"(limit={args.limit})", flush=True)

    rows = []
    skipped = 0
    for i, session in enumerate(sessions, 1):
        sdf = load_session_frame(args.cache_dir, session)
        if sdf is None or not len(sdf):
            skipped += 1
            continue
        frames = _ticker_frames(sdf)
        row = session_curves(frames, session)
        if row is None:
            skipped += 1
            # no-peek: structural counts only, no curve values
            print(f"[bflow-p1f] {session}: skipped (ineligible) ({i}/{len(sessions)})",
                  flush=True)
            continue
        rows.append(row)
        # no-peek: only structural counts — never print curve values
        print(f"[bflow-p1f] {session}: n_valid_minutes={row['n_valid_minutes']} "
              f"({i}/{len(sessions)})", flush=True)

    n_sessions = len(rows)
    print(f"[bflow-p1f] eligible sessions: {n_sessions} "
          f"(skipped/missing: {skipped})", flush=True)

    # ---- pooled statistics ----
    if rows:
        stats = pooled_stats(rows)
    else:
        # empty DataFrames
        empty = pd.DataFrame({"mean": [], "t": [], "n": []})
        stats = {k: empty.copy() for k in ("curve_gross", "curve_net", "cost_curve", "dev")}

    # ---- verdict ----
    verd = verdict(stats["dev"], n_sessions)

    # ---- named shapes ----
    shapes = named_shapes(stats["curve_gross"], stats["dev"])

    # ---- bucket curves ----
    bkt = bucket_curves(rows)

    # ---- artifacts ----
    out_dir = os.path.join(args.analysis_dir, "bflow_phase1f")
    os.makedirs(out_dir, exist_ok=True)

    # curves.parquet: long format (session × minute)
    parquet_path = os.path.join(out_dir, "curves.parquet")
    if rows:
        long_rows = []
        for r in rows:
            sess = r["session"]
            for m in range(389):
                long_rows.append({
                    "session": sess,
                    "minute": m,
                    "gross": r["curve_gross"][m],
                    "net": r["curve_net"][m],
                    "cost": r["cost_curve"][m],
                    "dev": r["dev"][m],
                })
        df_long = pd.DataFrame(long_rows)
    else:
        df_long = pd.DataFrame(columns=["session", "minute",
                                         "gross", "net", "cost", "dev"])
    df_long.to_parquet(parquet_path, index=False)

    # ---- report ----
    def _fmt(x, decimals=4):
        if x is None:
            return "n/a"
        try:
            v = float(x)
        except (TypeError, ValueError):
            return "n/a"
        if not np.isfinite(v):
            return "n/a"
        return f"{v:+.{decimals}f}"

    gross_s = stats["curve_gross"]
    net_s = stats["curve_net"]
    cost_s = stats["cost_curve"]
    dev_s = stats["dev"]

    # TEST_MINUTES table
    tm_header = ("| minute | mean gross (bps) | t(gross) | mean net (bps) "
                 "| mean cost (bps) | mean dev (bps) | t(dev) |")
    tm_sep = "|---|---|---|---|---|---|---|"
    tm_rows = []
    for m in TEST_MINUTES:
        tm_rows.append(
            f"| {m} "
            f"| {_fmt(gross_s['mean'][m])} "
            f"| {_fmt(gross_s['t'][m])} "
            f"| {_fmt(net_s['mean'][m])} "
            f"| {_fmt(cost_s['mean'][m])} "
            f"| {_fmt(dev_s['mean'][m])} "
            f"| {_fmt(dev_s['t'][m])} |"
        )

    # bucket table
    bkt_header = "| bucket | " + " | ".join(str(m) for m in TEST_MINUTES) + " |"
    bkt_sep = "|---|" + "|".join(["---"] * len(TEST_MINUTES)) + "|"
    bkt_rows_md = []
    from research.bflow.mr_policy import BUCKETS as _BUCKETS
    for name, _, _ in _BUCKETS:
        vals = bkt.get(name, {})
        cells = [_fmt(vals.get(m, float("nan"))) for m in TEST_MINUTES]
        bkt_rows_md.append(f"| {name} | " + " | ".join(cells) + " |")

    # named shapes section
    soa = shapes["shorts_at_open"]
    lpo = shapes["longs_post_open"]
    shape_lines = [
        "## Named shapes (descriptive)",
        "",
        "### (i) Shorts at the open — m ∈ {0..5}",
        "",
        "| minute | t(gross) |",
        "|---|---|",
    ]
    for m, tv in sorted(soa["t_values"].items()):
        shape_lines.append(f"| {m} | {_fmt(tv)} |")
    shape_lines += [
        "",
        f"- Significantly negative (t ≤ −{T_PASS} at ≥4 of 6): "
        f"{'YES' if soa['significant_negative'] else 'NO'} "
        f"(n={soa['n_significant_negative']})",
        "",
        "### (ii) Longs slightly after the open — local minimum m ∈ [10, 45]",
        "",
        f"- argmin minute: {lpo['argmin_m']}",
        f"- min gross value: {_fmt(lpo['min_value_bps'])} bps",
        f"- curve(5) value: {_fmt(lpo['curve5_value_bps'])} bps",
        f"- deeper than curve(5): {'YES' if lpo['deeper_than_curve5'] else 'NO'}",
        f"- |t(dev)| ≥ {T_PASS} at argmin: {'YES' if lpo['dev_t_significant'] else 'NO'}",
    ]

    lines = [
        "# SP-6 B-flow Phase-1f — Intraday Drift Atlas Report",
        "",
        "**Spec**: docs/superpowers/specs/2026-06-07-sp6-bflow-phase1f-drift-atlas-prereg.md",
        "",
        "**Decision linkage**: descriptive; nothing goes live; feeds the open-fill "
        "backtest variant.",
        "",
        "## Summary",
        "",
        f"- n_sessions (eligible): {n_sessions}",
        f"- n_sessions (skipped/missing): {skipped}",
        f"- MIN_VALID_MINUTES: {MIN_VALID_MINUTES}  |  "
        f"N_XS_MIN: {N_XS_MIN}  |  "
        f"MIN_SESSIONS: {MIN_SESSIONS}",
        "",
        "**Note on the uniform-accrual null**: under a strictly linear price path "
        "the dump window centers near m=387, not m=389. This induces a known "
        "systematic dev(m) ≈ −3m/389 × curve_gross(0)/dump, a monotone ramp of "
        "order 3 bps peak. FLAT requires the pre-named test-minute dev t-values "
        "to be insignificant; it does NOT require dev ≡ 0.",
        "",
        "## Pre-named test minutes",
        "",
        tm_header,
        tm_sep,
    ] + tm_rows + [
        "",
    ] + shape_lines + [
        "",
        "## Bucket diagnostics — mean dev at TEST_MINUTES (non-gating)",
        "",
        bkt_header,
        bkt_sep,
    ] + bkt_rows_md + [
        "",
        "## Verdict",
        "",
        f"**VERDICT: {verd}**",
        "",
        "- TIMING-STRUCTURE ⟺ ≥2 ADJACENT pre-named points with |t(D)| ≥ 3 and "
        "the same sign.",
        "- FLAT = no such adjacent pair.",
        "- INVALID-DATA = n_sessions < 700.",
        "",
        "Decision linkage (prereg §2): NOTHING goes live from this. "
        "TIMING-STRUCTURE feeds the open-fill backtest variant design; "
        "FLAT means fixed-time tweaks are not worth backtest-variant complexity "
        "beyond the plain open[t+1] case.",
    ]

    report = "\n".join(lines)
    report_path = os.path.join(out_dir, "report.md")
    with open(report_path, "w") as fh:
        fh.write(report)

    print(report, flush=True)
    print(f"[bflow-p1f] VERDICT: {verd}", flush=True)
    print(f"[bflow-p1f] wrote {report_path}", flush=True)
    print(f"[bflow-p1f] wrote {parquet_path}", flush=True)
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main(sys.argv[1:]))
