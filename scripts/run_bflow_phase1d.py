#!/usr/bin/env python3
"""SP-6 B-flow Phase-1d — MA-reversion entry policy runner (PRE-REGISTERED).

Spec (BINDING): docs/superpowers/specs/2026-06-06-sp6-bflow-phase1d-mr-policy-design.md
Two session-major passes over a minute-bar cache dir (CACHE-ONLY, never
fetches): pass 1 = policy rows; pass 2 = LOSO null accumulation (running sums
+ weighted guardrail histograms — plan amendment A1). Then scoring, stats,
guardrail, verdicts, report. Zero free parameters at eval time.

Usage:
    PYTHONPATH=src:. python3 scripts/run_bflow_phase1d.py \
        --cache-dir data/cache/min_bars_hist \
        --analysis-dir analysis/bflow_phase1d [--limit N]
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
    p = argparse.ArgumentParser(prog="run_bflow_phase1d")
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--analysis-dir", required=True)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args([] if argv is None else argv)

    import pandas as pd
    from research.bflow import mr_policy as mp
    from research.bflow.run_phase1b import (enumerate_cache_sessions,
                                            load_session_frame,
                                            _ticker_frames)

    sessions = enumerate_cache_sessions(args.cache_dir)
    if args.limit is not None:
        sessions = sessions[:args.limit]
    print(f"[bflow-p1d] cache {args.cache_dir}: {len(sessions)} sessions",
          flush=True)

    # ---- pass 1: policy rows (per-session compact DataFrames — A1) ----
    frames_list = []
    for i, s in enumerate(sessions, 1):
        sdf = load_session_frame(args.cache_dir, s)
        if sdf is None or not len(sdf):
            continue
        rows = mp.simulate_session_rows(_ticker_frames(sdf), session=s)
        if rows:
            sdf_rows = pd.DataFrame(rows)
            sdf_rows["entry_minute"] = sdf_rows["entry_minute"].astype("float64")
            frames_list.append(sdf_rows)
        print(f"[bflow-p1d][1] {s}: {len(rows)} rows ({i}/{len(sessions)})",
              flush=True)
    rows_df = (pd.concat(frames_list, ignore_index=True)
               if frames_list else pd.DataFrame())
    all_rows = rows_df.to_dict("records") if len(rows_df) else []

    # ---- weights from pass-1 triggered rows, then pass 2: LOSO null ----
    weights = mp.build_cell_weights(all_rows)
    acc = mp.NullAccumulator(cell_weights=weights)
    for i, s in enumerate(sessions, 1):
        sdf = load_session_frame(args.cache_dir, s)
        if sdf is None or not len(sdf):
            continue
        acc.add_records(mp.session_delta_records(_ticker_frames(sdf),
                                                 session=s))
        print(f"[bflow-p1d][2] {s}: null accumulated ({i}/{len(sessions)})",
              flush=True)

    # ---- scoring + stats + guardrail + verdicts ----
    scored, excluded = mp.score_rows(all_rows, acc)
    stats = mp.cell_stats(scored)
    guard = mp.guardrail_stats(scored, acc)
    verdicts = mp.leg_verdicts(stats, guard)
    diag = mp.diagnostics(scored)
    print(f"[bflow-p1d] excluded_thin_null={excluded}", flush=True)

    os.makedirs(args.analysis_dir, exist_ok=True)
    rows_df.to_parquet(os.path.join(args.analysis_dir, "policy_rows.parquet"))

    lines = ["# Phase-1d MA-reversion policy — report", "",
             f"sessions: {len(sessions)}; rows: {len(all_rows)}; "
             f"excluded(thin null): {excluded}", "",
             "| leg | zeta | mean_excess_bps | t | n_sessions | trig_rate | "
             "mean_dvs_dump | pol_p95_adv | pool_p95_adv |",
             "|---|---|---|---|---|---|---|---|---|"]
    for leg in mp.LEGS:
        for z in mp.ZETAS:
            c = stats.get((leg, z), {})
            g = guard.get((leg, z), {})
            lines.append(
                f"| {leg} | {z} | {c.get('mean_excess_bps', float('nan')):.3f} "
                f"| {c.get('t', float('nan')):.2f} | {c.get('n_sessions', 0)} "
                f"| {c.get('trigger_rate', float('nan')):.3f} "
                f"| {c.get('mean_delta_vs_dump_bps', float('nan')):.2f} "
                f"| {g.get('policy_p95_adverse', float('nan')):.1f} "
                f"| {g.get('pool_p95_adverse', float('nan')):.1f} |")
    lines += ["", "## Diagnostics (non-gating, spec §3)", ""]
    for (leg, z), d in sorted(diag.items()):
        lines.append(
            f"- {leg} z={z}: fallback={d['fallback_rate']:.3f}; "
            f"P(adv>5/10/25bps)={d['p_adverse_5']:.3f}/"
            f"{d['p_adverse_10']:.3f}/{d['p_adverse_25']:.3f}; "
            f"entry-minute d10/50/90={d['entry_minute_deciles']}")
        for name, b in d["buckets"].items():
            lines.append(f"    - {name}: mean_excess="
                         f"{b['mean_excess_bps']:.3f}bps "
                         f"(n={b['n_sessions']})")
    lines += ["", f"**VERDICT: LONG={verdicts.get('LONG', 'FAIL')} "
                  f"SHORT={verdicts.get('SHORT', 'FAIL')}**", "",
              "Quantization note (plan A1): guardrail pool p95 read from a "
              "0.1bps-bin histogram — accepted approximation vs the "
              "pre-registered 10bps margin.", "",
              "Linkage (spec §0): PASS authorizes the FORWARD SHADOW LANE "
              "only — never live cutover. FAIL closes the idea at minute "
              "scale. PASS-WITH-TAIL-BREACH does not authorize the lane."]
    report = "\n".join(lines)
    with open(os.path.join(args.analysis_dir, "report.md"), "w") as fh:
        fh.write(report)
    print(report, flush=True)
    print(f"[bflow-p1d] VERDICT LONG={verdicts.get('LONG', 'FAIL')} "
          f"SHORT={verdicts.get('SHORT', 'FAIL')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
