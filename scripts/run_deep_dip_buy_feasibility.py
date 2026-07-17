"""SP-6 deep-dip buy-leg feasibility runner.

Iterates the frozen minute-bar cache, computes per-(ticker, session, dip)
deep-dip buy improvement, aggregates with session-clustered t-stats, and writes
analysis/deep_dip_buy_feasibility/{report.md, rows.parquet}.

NO-PEEK: progress prints counts only; the verdict block is the first look.
Real cache pass is OPERATOR-ONLY; tests use synthetic in-memory frames.

Usage:
  python3 scripts/run_deep_dip_buy_feasibility.py [--limit N] [--analysis-dir X] ...

Spec: docs/archive/superpowers/specs/2026-06-08-sp6-deep-dip-buy-feasibility-prereg.md
"""
from __future__ import annotations

import argparse
import math
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import pandas as pd  # noqa: E402

from research.exit_timing.deep_dip_buy_feasibility import (  # noqa: E402
    DIP_LEVELS,
    HS_C,
    MIN_SESSIONS,
    PRIMARY_DIP,
    _fill_rate,
    _touch_fraction,
    clustered_t,
    event_deep_dip,
    verdict,
)


def load_universe(universe_path: str) -> set[str]:
    p = pathlib.Path(universe_path)
    if not p.exists():
        print(f"[deepdip] WARNING: universe file not found: {p}", flush=True)
        return set()
    return {line.strip() for line in p.read_text().splitlines() if line.strip()}


def iter_sessions(cache_dir: str, limit: int | None = None):
    pq_dir = pathlib.Path(cache_dir)
    files = sorted(pq_dir.glob("min_bars_*.parquet"))
    if limit is not None:
        files = files[:limit]
    for f in files:
        date_str = f.stem.replace("min_bars_", "")
        yield date_str, pd.read_parquet(f)


def _split_stats(rows: list[dict]):
    """Return blended / fill-only / nofill-only session-clustered improvement."""
    blended = clustered_t(rows, "improvement")
    fill_rows = [r for r in rows if r.get("filled")]
    nofill_rows = [r for r in rows if not r.get("filled")]
    return {
        "blended": blended,
        "fill": clustered_t(fill_rows, "improvement"),
        "nofill": clustered_t(nofill_rows, "improvement"),
        "fill_rate": _fill_rate(rows),
        "touch_fraction": _touch_fraction(rows),
        "open_to_close": clustered_t(rows, "open_to_close"),
    }


def _t_fmt(t: float) -> str:
    return "nan" if (t is None or (isinstance(t, float) and math.isnan(t))) else f"{t:+.3f}"


def render_report(per_dip: dict, n_sessions: int, verd: str) -> str:
    L = []
    L.append("# SP-6 Deep-Dip Buy-Leg Feasibility Study\n")
    L.append("**Prereg**: docs/archive/superpowers/specs/2026-06-08-sp6-deep-dip-buy-feasibility-prereg.md\n")
    L.append(
        "Deep-THROUGH buy limit (50 bps below the open). Unlike the at-touch "
        "passive sell, a fill here is genuine (the market crosses the level), so "
        "a positive is NOT inherently live-shadow-only — the over-fill artifact "
        "is confined to the touch-boundary fraction, reported below.\n"
    )
    L.append(f"Sessions analysed: {n_sessions}  (threshold for VALID: {MIN_SESSIONS})\n")
    L.append(f"Decision number = blended improvement at DIP = {PRIMARY_DIP:.0f} bps.\n")

    for dip in sorted(per_dip):
        s = per_dip[dip]
        b_m, b_t, b_n = s["blended"]
        f_m, f_t, f_n = s["fill"]
        nf_m, nf_t, nf_n = s["nofill"]
        oc_m, oc_t, _ = s["open_to_close"]
        star = "  ⟵ PRIMARY (verdict)" if dip == PRIMARY_DIP else ""
        L.append(f"---\n## DIP = {dip:.0f} bps{star}\n")
        L.append("| quantity | mean bps | t | n_sessions |")
        L.append("|----------|----------|---|------------|")
        L.append(f"| **improvement (blended)** | {b_m:+.4f} | {_t_fmt(b_t)} | {b_n} |")
        L.append(f"| improvement \\| fill | {f_m:+.4f} | {_t_fmt(f_t)} | {f_n} |")
        L.append(f"| improvement \\| no-fill | {nf_m:+.4f} | {_t_fmt(nf_t)} | {nf_n} |")
        L.append(f"| open→close drift (context) | {oc_m:+.4f} | {_t_fmt(oc_t)} | — |")
        L.append("")
        fr = s["fill_rate"]
        tf = s["touch_fraction"]
        L.append(f"- fill_rate: {fr:.3f}" if not math.isnan(fr) else "- fill_rate: nan")
        L.append(
            f"- touch_fraction (over-fill-ambiguous share of fills): {tf:.3f}"
            if not math.isnan(tf) else "- touch_fraction: nan"
        )
        L.append("")

    L.append("---\n## Verdict\n")
    p = per_dip[PRIMARY_DIP]
    pm, pt, pn = p["blended"]
    L.append(f"(DIP = {PRIMARY_DIP:.0f} bps — the operator's 0.5%.)\n")
    L.append(f"- blended improvement: {pm:+.4f} bps (t {_t_fmt(pt)}, n={pn})")
    L.append(f"- fill_rate: {p['fill_rate']:.3f}")
    L.append(f"- touch_fraction: {p['touch_fraction']:.3f}")
    L.append("")
    L.append(f"**VERDICT: {verd}**\n")
    L.append("### Decision linkage\n")
    L.append(
        "- **KILL**: even with the realized open as a free reference, the "
        "deep-dip buy loses to the close baseline — continuation after the dip "
        "and/or too-rare fills dominate. The buy leg of the final structure is "
        "closed WITH REAL DATA.\n"
        "- **MEASURED-POSITIVE**: a genuine, bar-measurable conditional edge "
        "(t≥3, fills predominantly clear-through). Unlike the at-touch passive "
        "sell, NOT inherently live-shadow-only → warrants a realizability "
        "follow-up (PM-spread HS_C∈{1,3}, touch-band discount, open-estimation "
        "error). NOT an automatic go-live.\n"
        "- **MARGINAL**: positive but t<3 OR touch_fraction≥0.5 (over-fill-"
        "tainted) → operator call.\n"
        "- **INVALID-DATA**: < 600 eligible sessions.\n"
    )
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="SP-6 deep-dip buy-leg feasibility study")
    ap.add_argument("--cache-dir", default="data/cache/min_bars_hist")
    ap.add_argument("--analysis-dir", default="analysis")
    ap.add_argument("--universe", default="analysis/bflow_phase1b_hist/universe_505.txt")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    universe = load_universe(args.universe)
    print(f"[deepdip] universe: {len(universe)} tickers", flush=True)

    per_dip_rows: dict[float, list[dict]] = {d: [] for d in DIP_LEVELS}
    sessions_seen: set[str] = set()
    total_events = 0

    for i, (date_str, sdf) in enumerate(iter_sessions(args.cache_dir, args.limit)):
        for ticker, tdf in sdf.groupby("ticker", sort=False):
            if universe and ticker not in universe:
                continue
            tdf = tdf.sort_values("minute").reset_index(drop=True)
            for dip in DIP_LEVELS:
                ev = event_deep_dip(tdf, dip, HS_C)
                if ev is None:
                    continue
                row = {"session": date_str, "ticker": ticker, "dip": dip}
                row.update(ev)
                per_dip_rows[dip].append(row)
                sessions_seen.add(date_str)
                total_events += 1
        print(f"[deepdip] {i + 1} sessions ({total_events} events)", end="\r", flush=True)

    n_sessions = len(sessions_seen)
    print(f"\n[deepdip] done: {n_sessions} eligible sessions, {total_events} events", flush=True)

    per_dip = {d: _split_stats(per_dip_rows[d]) for d in DIP_LEVELS}

    primary = per_dip[PRIMARY_DIP]
    verd = verdict(primary["blended"], primary["touch_fraction"], n_sessions)

    report_md = render_report(per_dip, n_sessions, verd)

    out_dir = pathlib.Path(args.analysis_dir) / "deep_dip_buy_feasibility"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(report_md)

    all_rows = [r for rows in per_dip_rows.values() for r in rows]
    if all_rows:
        pd.DataFrame(all_rows).to_parquet(out_dir / "rows.parquet", index=False)
        print(f"[deepdip] wrote {len(all_rows)} rows to {out_dir}/rows.parquet", flush=True)

    print(report_md, flush=True)
    print(f"[deepdip] VERDICT: {verd}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
