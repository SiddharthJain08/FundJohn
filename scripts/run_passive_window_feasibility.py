"""SP-6 passive window feasibility runner.

Iterates the frozen minute-bar cache, computes per-(ticker, session, hs)
improvement metrics, aggregates with session-clustered t-stats, and writes
analysis/passive_window_feasibility/{report.md, rows.parquet}.

NO-PEEK: progress prints counts only; verdict is the first look.
Real cache pass is OPERATOR-ONLY; tests use synthetic in-memory frames.

Usage:
  python3 scripts/run_passive_window_feasibility.py [--limit N] [--analysis-dir X] ...

Spec: docs/archive/superpowers/specs/2026-06-08-sp6-passive-window-feasibility-prereg.md
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

from research.exit_timing.passive_window_feasibility import (  # noqa: E402
    HS_CLOSE,
    HS_LEVELS,
    MIN_SESSIONS,
    clustered_t,
    event_improvements,
    verdict,
)


# ── data loading ──────────────────────────────────────────────────────────

def load_universe(universe_path: str) -> set[str]:
    p = pathlib.Path(universe_path)
    if not p.exists():
        print(f"[passive] WARNING: universe file not found: {p}", flush=True)
        return set()
    return {line.strip() for line in p.read_text().splitlines() if line.strip()}


def iter_sessions(cache_dir: str, limit: int | None = None):
    """Yield (date_str, DataFrame) sorted by filename."""
    pq_dir = pathlib.Path(cache_dir)
    files = sorted(pq_dir.glob("min_bars_*.parquet"))
    if limit is not None:
        files = files[:limit]
    for f in files:
        # min_bars_YYYY-MM-DD.parquet → YYYY-MM-DD
        date_str = f.stem.replace("min_bars_", "")
        df = pd.read_parquet(f)
        yield date_str, df


# ── aggregation helpers ───────────────────────────────────────────────────

METRIC_KEYS = [
    "sell_naive", "sell_oracle",
    "buy_mkt_naive", "buy_mkt_oracle",
    "buy_pass_naive",
    "combined_hybrid", "combined_all_passive",
]
FILL_KEYS = ["sell_fill_morning", "sell_fill_afternoon", "buy_pass_fill"]


def _fill_rate(rows: list[dict], key: str) -> float:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return float("nan")
    return sum(1.0 for v in vals if v) / len(vals)


def _sell_fill_total(rows: list[dict]) -> float:
    """Overall sell fill rate (morning or afternoon)."""
    if not rows:
        return float("nan")
    n = 0
    filled = 0
    for r in rows:
        n += 1
        if r.get("sell_fill_morning") or r.get("sell_fill_afternoon"):
            filled += 1
    return filled / n if n else float("nan")


# ── report rendering ──────────────────────────────────────────────────────

def _t_fmt(t: float) -> str:
    return "nan" if math.isnan(t) else f"{t:+.3f}"


def _stat_row(label: str, mean: float, t: float, n: int) -> str:
    return f"| {label} | {mean:+.4f} | {_t_fmt(t)} | {n} |"


def _calib_check(buy_mkt_naive_mean_bps: float, hs: float) -> str:
    """§3 calibration check: buy_mkt_naive at hs=5 should be ≈ −2 to −5 bps."""
    if hs != 5.0:
        return ""
    lo, hi = -5.0, -2.0
    status = "PASS" if lo <= buy_mkt_naive_mean_bps <= hi else "FAIL"
    return (
        f"\n**Calibration check (§3, hs=5):** "
        f"buy_mkt_naive = {buy_mkt_naive_mean_bps:+.3f} bps "
        f"(expected ≈ −2 to −5 bps) → **{status}**"
    )


def render_report(
    per_hs: dict[float, dict],
    n_sessions_total: int,
    verd: str,
) -> str:
    lines = []
    lines.append("# SP-6 Passive Window Feasibility Study\n")
    lines.append("**Prereg**: docs/archive/superpowers/specs/2026-06-08-sp6-passive-window-feasibility-prereg.md\n")
    lines.append(
        "Kill-only instrument: minute bars over-fill passive limits and cannot "
        "see adverse selection or queue position.  A NEGATIVE result is a "
        "decisive KILL; a POSITIVE result is INCONCLUSIVE — it would only "
        "motivate a live shadow of real passive fills, never a go-live decision.\n"
    )
    lines.append(f"Sessions analysed: {n_sessions_total}  "
                 f"(threshold for VALID: {MIN_SESSIONS})\n")

    for hs in sorted(per_hs):
        d = per_hs[hs]
        lines.append(f"---\n## hs = {hs} bps  (hs_close = {HS_CLOSE} bps)\n")
        lines.append("### Improvement metrics (bps vs close baseline, session-clustered)\n")
        lines.append("| metric | mean bps | t | n_sessions |")
        lines.append("|--------|----------|---|------------|")
        for key in METRIC_KEYS:
            stats = d["stats"][key]
            lines.append(_stat_row(key, stats[0], stats[1], stats[2]))
        lines.append("")

        lines.append("### Fill rates\n")
        fr = d["fill_rates"]
        lines.append("| fill event | rate |")
        lines.append("|------------|------|")
        for fk in FILL_KEYS:
            r = fr.get(fk, float("nan"))
            lines.append(f"| {fk} | {r:.3f} |" if not math.isnan(r) else f"| {fk} | nan |")
        st = fr.get("sell_fill_total", float("nan"))
        lines.append(f"| sell_fill_total (morning OR afternoon) | "
                     f"{st:.3f} |" if not math.isnan(st) else
                     "| sell_fill_total (morning OR afternoon) | nan |")
        lines.append("")

        lines.append("### COMBINED views\n")
        h_stats = d["stats"]["combined_hybrid"]
        ap_stats = d["stats"]["combined_all_passive"]
        lines.append("| combined view | mean bps | t | n_sessions |")
        lines.append("|---------------|----------|---|------------|")
        lines.append(_stat_row("HYBRID (passive-sell + marketable-buy)", *h_stats))
        lines.append(_stat_row("ALL-PASSIVE (passive-sell + passive-buy)", *ap_stats))
        lines.append("")

        # calibration check printed at hs=5 only
        if hs == 5.0:
            buy_mkt_mean = d["stats"]["buy_mkt_naive"][0]
            lines.append(_calib_check(buy_mkt_mean, hs))
            lines.append("")

    lines.append("---\n## Verdict\n")
    lines.append("(Evaluated at hs = 5.0 bps — the realistic level per the prereg.)\n")
    sell_mean = per_hs[5.0]["stats"]["sell_naive"][0]
    hybrid_mean = per_hs[5.0]["stats"]["combined_hybrid"][0]
    ap_mean = per_hs[5.0]["stats"]["combined_all_passive"][0]
    lines.append(f"- sell_naive mean: {sell_mean:+.4f} bps")
    lines.append(f"- combined_hybrid mean: {hybrid_mean:+.4f} bps")
    lines.append(f"- combined_all_passive mean: {ap_mean:+.4f} bps")
    lines.append("")
    lines.append(f"**VERDICT: {verd}**\n")

    lines.append("### Decision linkage\n")
    lines.append(
        "- **PARK**: buy drag dominates even this over-optimistic model; "
        "the hybrid (passive-sell + marketable-buy) is closed with the same "
        "finality as the marketable spread study.  No further work on this hybrid.\n"
        "- **INCONCLUSIVE-LEAN-SELL**: the sell-side upper bound exceeds the buy "
        "drag.  The ONLY warranted next step is a **live shadow of real passive "
        "sells** (or quote/queue data).  This is NOT a green light.  Do not "
        "go-live; do not proceed to queue-aware bar-sim (same data limit).  "
        "The all-passive variant (passive buys too) is the candidate for that shadow.\n"
        "- **INVALID-DATA**: fewer than 600 eligible sessions; result is unreliable.\n"
    )
    lines.append(
        "> **Caveat (§3 — pre-committed):** minute bars KILL-only.  "
        "positive = inconclusive, needs live shadow.  "
        "Bars cannot resolve passive provision, queue position, or adverse selection.\n"
    )

    return "\n".join(lines)


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="SP-6 passive window feasibility study")
    ap.add_argument("--cache-dir", default="data/cache/min_bars_hist",
                    help="Directory of min_bars_YYYY-MM-DD.parquet files")
    ap.add_argument("--analysis-dir", default="analysis",
                    help="Output root directory")
    ap.add_argument("--universe", default="analysis/bflow_phase1b_hist/universe_505.txt",
                    help="Path to universe ticker list (one per line)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Limit to first N sessions (smoke / dev mode)")
    args = ap.parse_args()

    universe = load_universe(args.universe)
    print(f"[passive] universe: {len(universe)} tickers", flush=True)

    # Collect raw rows indexed by hs
    per_hs_rows: dict[float, list[dict]] = {hs: [] for hs in HS_LEVELS}

    total_sessions = 0
    total_events = 0

    for i, (date_str, sdf) in enumerate(iter_sessions(args.cache_dir, args.limit)):
        session_events = 0
        # group by ticker
        for ticker, tdf in sdf.groupby("ticker", sort=False):
            if universe and ticker not in universe:
                continue
            tdf = tdf.sort_values("minute").reset_index(drop=True)
            for hs in HS_LEVELS:
                ev = event_improvements(tdf, hs, HS_CLOSE)
                if ev is None:
                    continue
                row = {"session": date_str, "ticker": ticker}
                row.update(ev)
                per_hs_rows[hs].append(row)
                session_events += 1

        total_sessions += 1
        total_events += session_events
        # NO-PEEK: counts only, never improvement values
        print(f"[passive] {i + 1}/{i + 1} sessions ({total_events} events)", end="\r", flush=True)

    print(f"\n[passive] done: {total_sessions} sessions, {total_events} events", flush=True)

    # Aggregate per hs
    per_hs_results: dict[float, dict] = {}
    for hs in HS_LEVELS:
        rows = per_hs_rows[hs]
        stats: dict[str, tuple] = {}
        for key in METRIC_KEYS:
            stats[key] = clustered_t(rows, key)
        fill_rates = {fk: _fill_rate(rows, fk) for fk in FILL_KEYS}
        fill_rates["sell_fill_total"] = _sell_fill_total(rows)
        per_hs_results[hs] = {"stats": stats, "fill_rates": fill_rates}

    # verdict at hs=5
    hs5 = per_hs_results[5.0]
    verd = verdict(
        combined_hybrid_stats=hs5["stats"]["combined_hybrid"],
        combined_allpassive_stats=hs5["stats"]["combined_all_passive"],
        sell_naive_stats=hs5["stats"]["sell_naive"],
        n_sessions=total_sessions,
    )

    # render report
    report_md = render_report(per_hs_results, total_sessions, verd)

    out_dir = pathlib.Path(args.analysis_dir) / "passive_window_feasibility"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(report_md)

    # save rows parquet for all hs values combined
    all_rows = []
    for hs, rows in per_hs_rows.items():
        for r in rows:
            r2 = dict(r)
            r2["hs"] = hs
            all_rows.append(r2)
    if all_rows:
        pd.DataFrame(all_rows).to_parquet(out_dir / "rows.parquet", index=False)
        print(f"[passive] wrote {len(all_rows)} rows to {out_dir}/rows.parquet", flush=True)

    print(report_md, flush=True)
    print(f"[passive] VERDICT: {verd}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
