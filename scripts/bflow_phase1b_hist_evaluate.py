#!/usr/bin/env python3
"""SP-6 B-flow — Phase-1b HISTORICAL KILL-TEST evaluator (mechanical).

Prereg: docs/superpowers/specs/2026-06-06-sp6-bflow-phase1b-historical-killtest-prereg.md

Reads ONE object — an ic_grid parquet (sessions x 15 cells, written by
run_phase1b) — applies the pre-committed eligibility + verdict rules of prereg
SS4-5 with ZERO free parameters, prints the verdict + grids, and writes
killtest_verdict.md next to the input.

Reuses the registered statistic verbatim via predictability.summarize
(across-session mean / sample-sd clustered t; session = cluster).

Usage:
    PYTHONPATH=src python3 scripts/bflow_phase1b_hist_evaluate.py \
        [--ic-grid analysis/bflow_phase1b_hist/bflow_phase1b_ic_grid.parquet] \
        [--no-write]   # dry-run mode (e.g. against the in-sample grid)
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
for _p in (_SRC, _ROOT):   # predictability imports via the `src.` prefix
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_IC_GRID = os.path.join(
    _ROOT, "analysis", "bflow_phase1b_hist", "bflow_phase1b_ic_grid.parquet")

WINDOW_START = "2023-01-03"
WINDOW_END = "2026-03-31"
MIN_ELIGIBLE_FOR_KILL = 700

PRIMARY = ("ofi_5|ret_to_dump", "ofi_15|ret_to_dump",
           "vwap_disp_30|ret_to_dump")
SECONDARY_HORIZONS = ("ret_fwd_5", "ret_fwd_15", "ret_fwd_30", "ret_fwd_60")

# 7 pre-committed calendar buckets (prereg SS4). Recent = last two.
BUCKETS = (
    ("2023H1", "2023-01-01", "2023-06-30"),
    ("2023H2", "2023-07-01", "2023-12-31"),
    ("2024H1", "2024-01-01", "2024-06-30"),
    ("2024H2", "2024-07-01", "2024-12-31"),
    ("2025H1", "2025-01-01", "2025-06-30"),
    ("2025H2", "2025-07-01", "2025-12-31"),
    ("2026Q1", "2026-01-01", "2026-03-31"),
)
RECENT_BUCKETS = ("2025H2", "2026Q1")


def _eligible(grid):
    """Prereg SS4: in-window AND >=1 non-NaN PRIMARY cell (early closes have
    all-3 PRIMARY NaN and are excluded WHOLESALE, secondaries included)."""
    idx = grid.index.astype(str)
    in_window = (idx >= WINDOW_START) & (idx <= WINDOW_END)
    has_primary = grid[list(PRIMARY)].notna().any(axis=1)
    return grid.loc[in_window & has_primary.values]


def _fmt(summary, cells):
    lines = [f"{'cell':<28} {'mean_ic':>9} {'t':>7} {'n':>5}"]
    for c in cells:
        r = summary.loc[c]
        lines.append(f"{c:<28} {r['mean_ic']:>9.4f} {r['t']:>7.2f} "
                     f"{int(r['n_sessions']):>5}")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="bflow_phase1b_hist_evaluate")
    p.add_argument("--ic-grid", default=DEFAULT_IC_GRID)
    p.add_argument("--no-write", action="store_true",
                   help="print only; do not write killtest_verdict.md")
    args = p.parse_args([] if argv is None else argv)

    import pandas as pd
    from research.bflow.predictability import summarize

    grid = pd.read_parquet(args.ic_grid)
    elig = _eligible(grid)
    n_elig = len(elig)
    out = [f"# Phase-1b historical kill-test verdict",
           f"",
           f"ic_grid: {args.ic_grid}",
           f"sessions in file: {len(grid)}; ELIGIBLE (prereg SS4): {n_elig}",
           f""]

    pooled = summarize(elig)
    out += ["## Pooled grid (all eligible sessions)", "```",
            _fmt(pooled, pooled.index), "```", ""]

    # per-bucket PRIMARY grids
    bucket_summaries = {}
    idx = elig.index.astype(str)
    for name, b0, b1 in BUCKETS:
        sub = elig.loc[(idx >= b0) & (idx <= b1)]
        if len(sub) == 0:
            out += [f"## {name}: 0 eligible sessions", ""]
            continue
        s = summarize(sub)
        bucket_summaries[name] = s
        out += [f"## {name} ({len(sub)} sessions) — PRIMARY", "```",
                _fmt(s, PRIMARY), "```", ""]

    # ---- verdict rules (prereg SS5) — sign-agnostic structure test ----
    pri = pooled.loc[list(PRIMARY)]
    pooled_hit = pri[pri["t"].abs() >= 3]

    recent_hit = False
    for name in RECENT_BUCKETS:
        s = bucket_summaries.get(name)
        if s is not None and (s.loc[list(PRIMARY)]["t"].abs() >= 2).any():
            recent_hit = True

    # SS5 GO-shape check (for SURVIVE-STRONG): >=2/3 PRIMARY |t|>=3 consistent
    # sign + >=2 secondary horizons |t|>=2 same sign.
    strong = None
    for sign in (-1, +1):
        n_pri = sum(1 for c in PRIMARY
                    if pooled.loc[c, "t"] * sign >= 3)
        if n_pri >= 2:
            n_sec = 0
            for h in SECONDARY_HORIZONS:
                hcells = [c for c in pooled.index if c.endswith("|" + h)]
                if any(pooled.loc[c, "t"] * sign >= 2 for c in hcells):
                    n_sec += 1
            if n_sec >= 2:
                strong = ("SURVIVE-STRONG" if sign < 0
                          else "SURVIVE-STRONG-INVERTED")

    if len(pooled_hit) == 0 and not recent_hit:
        verdict = ("KILL" if n_elig >= MIN_ELIGIBLE_FOR_KILL
                   else "INVALID-DATA")
    elif len(pooled_hit) == 0 and recent_hit:
        verdict = "SURVIVE-AMBIGUOUS"
    elif strong:
        verdict = strong
    else:
        verdict = "SURVIVE-WEAK"

    out += ["## VERDICT", "",
            f"- pooled PRIMARY cells with |t|>=3 (either sign): "
            f"{list(pooled_hit.index) or 'NONE'}",
            f"- recent-bucket ({'/'.join(RECENT_BUCKETS)}) PRIMARY |t|>=2: "
            f"{recent_hit}",
            f"- eligible sessions: {n_elig} "
            f"(KILL floor {MIN_ELIGIBLE_FOR_KILL})",
            "",
            f"**VERDICT: {verdict}**", "",
            "Linkage (prereg SS0/SS5): KILL => minute-scale flow channel "
            "closed, July forward decision pre-empted. Any SURVIVE => the "
            "registered forward gate (n_oos>=20, sessions >=2026-06-08) "
            "remains the SOLE PASS arbiter — no historical outcome can pass.",
            ""]

    report = "\n".join(out)
    print(report, flush=True)
    print(f"[bflow-hist-eval] VERDICT: {verdict}", flush=True)

    if not args.no_write:
        dest = os.path.join(os.path.dirname(os.path.abspath(args.ic_grid)),
                            "killtest_verdict.md")
        with open(dest, "w") as fh:
            fh.write(report)
        print(f"[bflow-hist-eval] wrote {dest}", flush=True)
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main(sys.argv[1:]))
