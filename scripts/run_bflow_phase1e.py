#!/usr/bin/env python3
"""SP-6 B-flow Phase-1e — cross-sectional discriminator runner (PRE-REGISTERED).

Spec (BINDING): docs/superpowers/specs/2026-06-07-sp6-bflow-phase1e-xsec-discriminator-prereg.md
Single session-major pass over a minute-bar cache dir (CACHE-ONLY, never
fetches). Per session: within-minute decile L/S on vwap_disp_30, gross and
net of differential spread cost. Clustered t + verdict. Zero free parameters.

Usage:
    PYTHONPATH=src:. python3 scripts/run_bflow_phase1e.py \\
        --cache-dir data/cache/min_bars_hist \\
        --analysis-dir analysis/bflow_phase1e [--limit N]
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
    p = argparse.ArgumentParser(prog="run_bflow_phase1e")
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--analysis-dir", required=True)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args([] if argv is None else argv)

    import numpy as np
    import pandas as pd
    from research.bflow.xsec_discriminator import (
        session_spreads, clustered_t, verdict, bucket_table)
    from research.bflow.run_phase1b import (enumerate_cache_sessions,
                                            load_session_frame,
                                            _ticker_frames)

    sessions = enumerate_cache_sessions(args.cache_dir)
    if args.limit is not None:
        sessions = sessions[:args.limit]
    print(f"[bflow-p1e] cache {args.cache_dir}: {len(sessions)} sessions "
          f"(limit={args.limit})", flush=True)

    rows = []
    skipped = 0
    for i, session in enumerate(sessions, 1):
        sdf = load_session_frame(args.cache_dir, session)
        if sdf is None or not len(sdf):
            skipped += 1
            continue
        frames = _ticker_frames(sdf)
        row = session_spreads(frames, session)
        if row is None:
            skipped += 1
            # print no-peek progress without spread values
            print(f"[bflow-p1e] {session}: skipped (ineligible) ({i}/{len(sessions)})",
                  flush=True)
            continue
        rows.append(row)
        # no-peek: only structural counts, no spread values
        print(f"[bflow-p1e] {session}: n_minutes={row['n_minutes']} "
              f"n_xs~{row['mean_n_xs']:.0f} ({i}/{len(sessions)})", flush=True)

    n_sessions = len(rows)
    print(f"[bflow-p1e] eligible sessions: {n_sessions} "
          f"(skipped/missing: {skipped})", flush=True)

    # ---- stats ----
    gross_vals = [r["spread_gross"] for r in rows]
    net_vals = [r["spread_net"] for r in rows]
    lx_vals = [r["long_excess_gross"] for r in rows]
    sx_vals = [r["short_excess_gross"] for r in rows]
    nxs_vals = [r["mean_n_xs"] for r in rows]

    gross_t = clustered_t(gross_vals)
    net_t = clustered_t(net_vals)

    verd = verdict(gross_t, net_t, n_sessions)

    btable = bucket_table(rows)

    # ---- artifacts ----
    out_dir = os.path.join(args.analysis_dir, "bflow_phase1e")
    os.makedirs(out_dir, exist_ok=True)
    parquet_path = os.path.join(out_dir, "spread_sessions.parquet")
    if rows:
        df_out = pd.DataFrame(rows)
    else:
        df_out = pd.DataFrame(columns=["session", "n_minutes", "spread_net",
                                        "spread_gross", "long_excess_gross",
                                        "short_excess_gross", "mean_n_xs"])
    df_out.to_parquet(parquet_path, index=False)

    # ---- report ----
    def _fmt(x, decimals=4):
        if x is None or not np.isfinite(float(x)):
            return "n/a"
        return f"{float(x):+.{decimals}f}"

    lines = [
        "# SP-6 B-flow Phase-1e — Cross-Sectional Discriminator Report",
        "",
        "**Spec**: docs/superpowers/specs/2026-06-07-sp6-bflow-phase1e-xsec-discriminator-prereg.md",
        "",
        "## Summary",
        "",
        f"- n_sessions (eligible): {n_sessions}",
        f"- n_sessions (skipped/missing): {skipped}",
        "",
        "## Statistics",
        "",
        f"- spread_gross: mean={_fmt(float(np.mean(gross_vals)) if gross_vals else float('nan'))} "
        f"bps, t={_fmt(gross_t)}",
        f"- spread_net: mean={_fmt(float(np.mean(net_vals)) if net_vals else float('nan'))} "
        f"bps, t={_fmt(net_t)}",
        f"- long_excess_gross: mean={_fmt(float(np.mean(lx_vals)) if lx_vals else float('nan'))} bps",
        f"- short_excess_gross: mean={_fmt(float(np.mean(sx_vals)) if sx_vals else float('nan'))} bps",
        f"- mean_n_xs: {_fmt(float(np.mean(nxs_vals)) if nxs_vals else float('nan'), 1)}",
        "",
        "## Per-bucket diagnostics (non-gating)",
        "",
        "| bucket | net (bps) | gross (bps) | n |",
        "|---|---|---|---|",
    ]
    for bname, bdata in sorted(btable.items()):
        lines.append(
            f"| {bname} | {_fmt(bdata['net'])} | {_fmt(bdata['gross'])} | {bdata['n']} |")
    lines += [
        "",
        "## Verdict",
        "",
        f"**VERDICT: {verd}**",
        "",
        "Decision linkage (prereg §2): NO outcome authorizes anything live; "
        "ECON-PASS justifies designing a forward-confirmable selection/portfolio "
        "use; NULL closes the cross-sectional question.",
    ]
    report = "\n".join(lines)
    report_path = os.path.join(out_dir, "report.md")
    with open(report_path, "w") as fh:
        fh.write(report)

    print(report, flush=True)
    print(f"[bflow-p1e] VERDICT: {verd}", flush=True)
    print(f"[bflow-p1e] wrote {report_path}", flush=True)
    print(f"[bflow-p1e] wrote {parquet_path}", flush=True)
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main(sys.argv[1:]))
